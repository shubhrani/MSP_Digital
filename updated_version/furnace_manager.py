"""
Dynamic furnace slot manager.

Owns the three physical StreamSources and the two logical FurnaceSlots
(4/1 and 4/2). At startup it probes every stream for class 28
(furnace_operation_undermaintenance) and treats the first one that shows
it as the current maintenance stream; the other two back the two slots,
in the order they appear in config.STREAMS.

PYRO belongs to the logical FurnaceSlot, not the physical stream.
When a stream gets swapped in for a slot, it inherits that slot's PYRO
identity and monitor.

Two-stage re-evaluation logic (Class 28 First, Active Fire Second):
1. Filter 1 — Class 28 (furnace_operation_undermaintenance): Hard reject.
   Any stream with class 28 is tagged 'maintenance' and disqualified.
2. Filter 2 — Class 3 (furnace_operation_activefire): Hot camera preference.
   Survivors without class 28 are probed for class 3 ('hot'). Only hot
   cameras are eligible.
3. If len(eligible) < 2, abort and retry on next tick.
4. Keep the healthy slot on its current camera if eligible; move failing slot
   to first remaining eligible. Fall back to fixed slot order if both disqualified.

Three triggers for re-evaluation:
- Trigger 1: Live heat loses active fire (5-min watchdog in state machine).
- Trigger 2: Idle slot with stale activefire heartbeat (watchdog tick).
- Trigger 3: Class 28 detected at runtime on slot's camera.
"""

import datetime
import logging
import threading
import time

import cv2

import config
from camera_worker import CameraWorker
from debounce import ClassDebouncer
from state_machine import (
    HeatStateMachine,
    CLS_FURNACE_ACTIVEFIRE,
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
)
from temperature_monitor import TemperatureMonitor

logger = logging.getLogger(__name__)


class StreamSource:
    """One physical camera. No PYRO — PYRO belongs to the slot."""

    def __init__(self, cfg):
        self.stream_id = cfg["stream_id"]
        self.name = cfg["name"]
        self.rtsp_url = cfg["rtsp_url"]

        # "unknown" | "active" | "spare" | "maintenance" | "faulty"
        self.status = "unknown"
        self.assigned_slot = None   # slug of the slot currently using it, or None
        self.history = []           # small log of status changes for the UI

    def mark(self, status, note=""):
        if status != self.status:
            self.status = status
            self.history.append({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "status": status,
                "note": note,
            })


class FurnaceSlot:
    """One logical furnace slot. Owns its TemperatureMonitor (PYRO belongs
    to the slot and survives camera swaps), state machine, and (transiently)
    a CameraWorker pointed at whichever stream is assigned to it."""

    def __init__(self, slug, cfg, recorder, on_activefire_loss):
        self.slug = slug
        self.furnace_id = cfg["furnace_id"]
        self.name = cfg["name"]
        self.data_dir = cfg["data_dir"]
        self.index_csv = cfg["index_csv"]
        self.heat_id_prefix = cfg["heat_id_prefix"]
        self.recorder = recorder
        self._on_activefire_loss = on_activefire_loss

        # PYRO monitor is created ONCE, belongs to the slot, survives swaps.
        self.temperature_monitor = TemperatureMonitor(
            group=cfg["influx_group"],
            field=cfg["pyro_field"],
        )
        self.pyro_field = cfg["pyro_field"]

        self.current_stream = None
        self.state_machine = None
        self.camera_worker = None
        self._swap_in_progress = False

    def start_monitor(self):
        self.temperature_monitor.start()

    def stop_monitor(self):
        try:
            self.temperature_monitor.stop()
        except Exception:
            pass

    def attach_to_stream(self, stream):
        """(Re)start the state machine + camera worker on a new stream.
        The PYRO monitor is NOT touched — it stays with the slot."""
        self.detach()

        self.current_stream = stream
        stream.assigned_slot = self.slug
        stream.mark("active", f"assigned to {self.slug}")

        self.state_machine = HeatStateMachine(
            data_dir=self.data_dir,
            index_csv=self.index_csv,
            recorder=self.recorder,
            furnace_id=self.furnace_id,
            heat_id_prefix=self.heat_id_prefix,
            temperature_monitor=self.temperature_monitor,   # slot's own monitor
            on_activefire_loss=self._handle_activefire_loss,
            on_maintenance_detected=self._handle_maintenance_detected,
        )
        self.camera_worker = CameraWorker(
            state_machine=self.state_machine,
            rtsp_url=stream.rtsp_url,
            stream_id=stream.stream_id,
        )
        self.camera_worker.start()
        logger.info("Slot %s bound to stream %s (PYRO %s)",
                    self.slug, stream.stream_id, self.pyro_field)

    def detach(self):
        if self.camera_worker is not None:
            try:
                self.camera_worker.stop()
            except Exception:
                logger.exception("Failed to stop camera worker for %s", self.slug)
            self.camera_worker = None
        if self.current_stream is not None:
            self.current_stream.assigned_slot = None
            self.current_stream = None
        self.state_machine = None

    def _handle_activefire_loss(self):
        # Called from inside the camera worker thread. Don't do the swap
        # synchronously (we'd be stopping our own thread); hand it to the
        # manager which will spawn a fresh thread.
        if self._swap_in_progress:
            return
        self._swap_in_progress = True
        try:
            self._on_activefire_loss(self)
        finally:
            pass

    def _handle_maintenance_detected(self):
        # Trigger 3: class 28 detected at runtime.
        if self._swap_in_progress:
            return
        logger.warning("Slot %s camera reported class 28 (maintenance) - requesting swap", self.slug)
        self._swap_in_progress = True
        try:
            self._on_activefire_loss(self)
        finally:
            pass

    def swap_done(self):
        self._swap_in_progress = False


class FurnaceManager:
    def __init__(self, recorder):
        self.recorder = recorder
        self.streams = [StreamSource(cfg) for cfg in config.STREAMS]
        self.slots = {
            slug: FurnaceSlot(slug, cfg, recorder,
                              on_activefire_loss=self._on_slot_lost_activefire)
            for slug, cfg in config.FURNACE_SLOTS.items()
        }
        self._lock = threading.RLock()
        self._reeval_thread = None
        self._poll_stop = threading.Event()
        self._poll_thread = None

    # ----------------------------------------------------------------
    # Startup
    # ----------------------------------------------------------------
    def start(self):
        # Start each slot's PYRO monitor exactly once, up-front, and never
        # again — the monitor's lifetime spans every stream swap.
        for slot in self.slots.values():
            slot.start_monitor()

        maintenance = self._probe_for_maintenance()
        maintenance_ids = {s.stream_id for s in maintenance}
        for s in self.streams:
            if s.stream_id in maintenance_ids:
                s.mark("maintenance", "class 28 confirmed during startup probe")

        available = [s for s in self.streams if s.stream_id not in maintenance_ids]
        logger.info("Startup probe done. maintenance=%s available=%s",
                    list(maintenance_ids), [s.stream_id for s in available])

        order = ["4-1", "4-2"]
        for i, slug in enumerate(order):
            if i < len(available) and slug in self.slots:
                self.slots[slug].attach_to_stream(available[i])
            else:
                logger.error("Not enough available streams to assign %s", slug)

        for s in available[len(order):]:
            s.mark("spare", "not assigned to a slot")

        # NEW: start the idle-slot activefire watchdog.
        self._poll_thread = threading.Thread(
            target=self._watchdog_loop,
            name="ActiveFireWatchdog", daemon=True,
        )
        self._poll_thread.start()

    def stop(self):
        self._poll_stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        for slot in self.slots.values():
            slot.detach()
            slot.stop_monitor()

    # ----------------------------------------------------------------
    # Watchdog & Re-evaluation
    # ----------------------------------------------------------------
    def _watchdog_loop(self):
        """Every SLOT_POLL_SECONDS, check every assigned slot for a stale
        last_activefire_at. The state machine owns this check while a heat
        is live (it wipes the heat and calls us back). This loop covers
        the OTHER case: the slot is idle, the furnace behind it has gone
        cold, and the plant wants to bring the spare online."""
        while not self._poll_stop.wait(config.SLOT_POLL_SECONDS):
            try:
                self._watchdog_tick()
            except Exception:
                logger.exception("Active-fire watchdog tick failed")

    def _watchdog_tick(self):
        now = datetime.datetime.now()

        for slot in self.slots.values():
            sm = slot.state_machine
            if sm is None or slot.current_stream is None:
                continue

            # If a heat is live, the state machine's own watchdog fires and
            # calls _on_slot_lost_activefire. Don't double-trigger.
            if sm.state != sm.STATE_IDLE:
                continue

            last = sm.last_activefire_at
            if last is None:
                continue
            idle_minutes = (now - last).total_seconds() / 60.0
            if idle_minutes >= config.ACTIVEFIRE_TIMEOUT_MINUTES:
                logger.warning(
                    "Slot %s idle and no activefire for %.1f min on %s - "
                    "triggering stream re-evaluation",
                    slot.slug, idle_minutes, slot.current_stream.stream_id,
                )
                self._trigger_reevaluation(slot)
                # Only one slot at a time; the next tick handles the other
                # if it too has gone cold.
                return

    def _on_slot_lost_activefire(self, slot):
        """Called by a slot's state machine when it wipes a heat for lack
        of active fire."""
        self._trigger_reevaluation(slot)

    def _trigger_reevaluation(self, slot):
        if self._reeval_thread and self._reeval_thread.is_alive():
            return
        self._reeval_thread = threading.Thread(
            target=self._reevaluate_streams, args=(slot,),
            name=f"ReevalStreams-{slot.slug}", daemon=True,
        )
        self._reeval_thread.start()

    def _reevaluate_streams(self, failing_slot):
        """Two-stage filter re-evaluation:
        Step 1 — Class 28 (maintenance) probe across all streams in parallel.
                 Any stream with class 28 is tagged maintenance and disqualified.
        Step 2 — Class 3 (activefire) probe across the survivors in parallel.
                 Only streams with class 3 are tagged hot and eligible.
        Step 3 — Build eligible list; abort if len(eligible) < 2.
        Step 4 — Healthy slot keeps camera if eligible; failing slot takes next eligible.
                 If both disqualified, fall back to fixed slot order (4-1, 4-2).
        Step 5 — Apply swap, preserving maintenance status or marking spare.
        """
        with self._lock:
            logger.info("Re-evaluating all 3 streams after %s triggered re-evaluation",
                        failing_slot.slug)

            # ---- Step 1: Probe all streams for class 28 (priority 1) in parallel ----
            maintenance_results = {}
            maint_lock = threading.Lock()
            probe_seconds = getattr(config, "PROBE_ACTIVEFIRE_SECONDS", 8.0)

            def probe_maint(stream):
                is_maint = self._probe_stream(
                    stream.rtsp_url,
                    probe_seconds,
                    target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
                )
                with maint_lock:
                    maintenance_results[stream.stream_id] = is_maint

            threads = [
                threading.Thread(target=probe_maint, args=(s,), daemon=True)
                for s in self.streams
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=probe_seconds + 10)

            for s in self.streams:
                if maintenance_results.get(s.stream_id):
                    logger.warning("Stream %s confirmed class 28 - maintenance", s.stream_id)
                    s.mark("maintenance", "class 28 confirmed")

            survivors = [s for s in self.streams if not maintenance_results.get(s.stream_id)]

            # ---- Step 2: Probe survivors for class 3 (priority 2) in parallel ----
            activefire_results = {}
            fire_lock = threading.Lock()

            def probe_fire(stream):
                has_fire = self._probe_stream(
                    stream.rtsp_url,
                    probe_seconds,
                    target_class=CLS_FURNACE_ACTIVEFIRE,
                )
                with fire_lock:
                    activefire_results[stream.stream_id] = has_fire

            fire_threads = [
                threading.Thread(target=probe_fire, args=(s,), daemon=True)
                for s in survivors
            ]
            for t in fire_threads:
                t.start()
            for t in fire_threads:
                t.join(timeout=probe_seconds + 10)

            logger.info("Probe results: maintenance=%s, activefire=%s",
                        {s.stream_id: bool(maintenance_results.get(s.stream_id)) for s in self.streams},
                        {s.stream_id: bool(activefire_results.get(s.stream_id)) for s in survivors})

            # ---- Step 3: Build eligible list ----
            eligible = [s for s in survivors if activefire_results.get(s.stream_id)]

            if len(eligible) < 2:
                logger.warning(
                    "Only %d stream(s) eligible (not maintenance and active fire); leaving "
                    "current assignment alone. Will retry on next watchdog tick.",
                    len(eligible),
                )
                if failing_slot._swap_in_progress:
                    failing_slot.swap_done()
                return

            # ---- Step 4: Decide assignments ----
            other_slug = next(s for s in self.slots if s != failing_slot.slug)
            other_slot = self.slots[other_slug]

            assignment = {}

            if other_slot.current_stream in eligible:
                # The other slot's current camera is still in eligible -> it keeps it.
                # The failing slot gets the first remaining eligible camera.
                assignment[other_slug] = other_slot.current_stream
                remaining = [s for s in eligible if s is not other_slot.current_stream]
                if not remaining:
                    logger.warning("No eligible stream left to give %s", failing_slot.slug)
                    if failing_slot._swap_in_progress:
                        failing_slot.swap_done()
                    return
                assignment[failing_slot.slug] = remaining[0]
            else:
                # Both slots disqualified (or other slot also maintenance/cold) ->
                # fixed slot order
                assignment["4-1"] = eligible[0]
                assignment["4-2"] = eligible[1]

            # ---- Step 5: Apply the swap ----
            for slug, stream in assignment.items():
                slot = self.slots[slug]
                if slot.current_stream is stream:
                    continue  # already there

                # Detach from old stream and mark spare (or preserve maintenance)
                if slot.current_stream is not None:
                    old = slot.current_stream
                    old.assigned_slot = None
                    if old.status != "maintenance":
                        old.mark("spare", f"released by {slug} during re-evaluation")

                slot.attach_to_stream(stream)
                logger.info("Re-evaluation bound slot %s -> %s (PYRO %s)",
                            slug, stream.stream_id, slot.pyro_field)

            # Clear swap flag on failing slot
            if failing_slot._swap_in_progress:
                failing_slot.swap_done()

            # Any stream not assigned and not maintenance becomes spare
            assigned_ids = {s.stream_id for s in assignment.values()}
            for s in self.streams:
                if s.stream_id not in assigned_ids and s.assigned_slot is None:
                    if s.status != "maintenance":
                        s.mark("spare", "unused after re-evaluation")

    def _probe_for_maintenance(self):
        """Return at most one stream where class 28 was confirmed."""
        hits = []
        for stream in self.streams:
            logger.info("Probing %s (%s) for %ss...",
                        stream.stream_id, stream.rtsp_url, config.STREAM_PROBE_SECONDS)
            if self._probe_stream(stream.rtsp_url, config.STREAM_PROBE_SECONDS,
                                  target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE):
                logger.warning("Stream %s shows class 28 - maintenance",
                               stream.stream_id)
                hits.append(stream)
                break   # first hit wins; probe the rest lazily
        return hits

    def _probe_stream(self, rtsp_url, duration_seconds,
                      target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE):
        """Return True if target_class is confirmed on this stream within
        duration_seconds. Reused for class 28 (startup) and class 3
        (runtime re-evaluation)."""
        from detector import FurnaceDetector

        try:
            detector = FurnaceDetector()
        except Exception:
            logger.exception("Could not load model during stream probe")
            return False

        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 2000)
        if not cap.isOpened():
            logger.warning("Probe could not open %s", rtsp_url)
            cap.release()
            return False

        debouncer = ClassDebouncer(
            confirm_frames=config.DEBOUNCE_FRAMES,
            class_ids={target_class},
        )
        deadline = time.monotonic() + duration_seconds
        try:
            while time.monotonic() < deadline:
                ret, frame = cap.read()
                if not ret or frame is None:
                    break
                try:
                    detections = detector.detect(frame)
                except Exception:
                    continue
                confirmed, _ = debouncer.update(
                    {d["class_id"] for d in detections}
                )
                if target_class in confirmed:
                    return True
        finally:
            cap.release()
        return False

    # ----------------------------------------------------------------
    # Accessors
    # ----------------------------------------------------------------
    def get_slot(self, slug):
        return self.slots.get(slug)

    def nav_slots(self):
        return [{"slug": s.slug, "name": s.name} for s in self.slots.values()]

    def streams_snapshot(self):
        return [
            {
                "stream_id": s.stream_id,
                "name": s.name,
                "rtsp_url": s.rtsp_url,
                "status": s.status,
                "assigned_slot": s.assigned_slot,
                "history": s.history[-5:],
            }
            for s in self.streams
        ]
