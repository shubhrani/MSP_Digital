"""
Dynamic furnace slot manager.

Owns the three physical StreamSources and the two logical FurnaceSlots
(5/1 and 5/2). At startup it probes every stream for class 28
(furnace_operation_undermaintenance) and treats the first one that shows
it as the current maintenance stream; the other two back the two slots,
in the order they appear in config.STREAMS.

PYRO belongs to the logical FurnaceSlot, not the physical stream.
When a stream gets swapped in for a slot, it inherits that slot's PYRO
identity and monitor.

At runtime, if an active slot's stream has no class 5 (activefire) for
STREAM_ACTIVEFIRE_TIMEOUT_MINUTES, the state machine calls back here and
we swap that slot onto whichever stream is currently the spare. The other
slot is never touched - its heat, its CSV, its in-flight taps all keep
running.
"""

import datetime
import logging
import threading
import time

import cv2

import config
from camera_worker import (
    CameraWorker,
    MAINTENANCE_TRACKED_CLASS_IDS,
    maintenance_reason,
)
from debounce import ClassDebouncer
from state_machine import (
    HeatStateMachine,
    CLS_FURNACE_ACTIVEFIRE,
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
    CLS_FURNACE_POSITION_UP,                  # 34
    CLS_SPONGE_FEEDER_ENGAGED,                # 4
    CLS_SPONGE_FEEDER_ON_FURNACE,             # 15
    CLS_MAGNET_FEEDING_SCRAP,                 # 24
    CLS_FURNACE_TILTING,                      # 17
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,    # 18
    CLS_LADDLE_PLACED_FORTAPPING,             # 36
    CLS_HUMAN_FEEDING_SCRAP,                  # 37
)
from temperature_monitor import TemperatureMonitor

# Classes that mean the furnace is no longer under maintenance - the
# process is about to start, or has already started. Seeing any of these
# confirmed on a stream that was marked "maintenance" clears that flag.
#
# Class 5 (furnace_operation_activefire) is the top priority: an actual
# fire in the furnace is the strongest possible signal that the plant is
# running again, so it takes precedence over the other classes.
PROCESS_RESUME_CLASS_IDS = {
    CLS_FURNACE_ACTIVEFIRE,                  # 5  (top priority)
    CLS_FURNACE_POSITION_UP,                 # 34
    CLS_SPONGE_FEEDER_ENGAGED,               # 4
    CLS_SPONGE_FEEDER_ON_FURNACE,            # 15
    CLS_MAGNET_FEEDING_SCRAP,                # 24
    CLS_FURNACE_TILTING,                     # 17
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,   # 18
    CLS_LADDLE_PLACED_FORTAPPING,            # 36
    CLS_HUMAN_FEEDING_SCRAP,                 # 37
}

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

    def __init__(
        self,
        slug,
        cfg,
        recorder,
        on_activefire_loss,
        on_maintenance_detected=None,
    ):
        self.slug = slug
        self.furnace_id = cfg["furnace_id"]
        self.name = cfg["name"]
        self.data_dir = cfg["data_dir"]
        self.index_csv = cfg["index_csv"]
        self.heat_id_prefix = cfg["heat_id_prefix"]
        self.recorder = recorder
        self._on_activefire_loss = on_activefire_loss
        self._on_maintenance_detected = on_maintenance_detected

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
        )
        self.camera_worker = CameraWorker(
            state_machine=self.state_machine,
            rtsp_url=stream.rtsp_url,
            stream_id=stream.stream_id,
            on_maintenance_detected=self._handle_maintenance_detected,
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

    def _handle_maintenance_detected(self, reason):
        if (
            self._swap_in_progress
            or self._on_maintenance_detected is None
        ):
            return

        self._swap_in_progress = True

        try:
            self._on_maintenance_detected(self, reason)
        finally:
            pass

    def swap_done(self):
        self._swap_in_progress = False


class FurnaceManager:
    def __init__(self, recorder):
        self.recorder = recorder
        self.streams = [StreamSource(cfg) for cfg in config.STREAMS]
        self.slots = {
            slug: FurnaceSlot(
                slug,
                cfg,
                recorder,
                on_activefire_loss=self._on_slot_lost_activefire,
                on_maintenance_detected=self._on_slot_maintenance_detected,
            )
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
        max_maintenance = max(0, len(self.streams) - len(self.slots))

        # If more streams show maintenance than available spare slots (e.g. 2 streams
        # flagged maintenance when we have 3 streams and need 2 active slots):
        # We MUST NOT designate all of them as maintenance and leave a slot unassigned.
        # Direct maintenance classes (28, 31, 0, 39) take precedence over indirect fan heuristics.
        if len(maintenance) > max_maintenance:
            def _maint_priority(s):
                reason = getattr(s, "_probe_maintenance_reason", "") or ""
                # Priority 0: confirmed direct maintenance class (e.g. 28, 31)
                is_direct = any(f"class {cid}" in reason for cid in [28, 31, 0, 39])
                return (0 if is_direct else 1, s.stream_id)

            sorted_maint = sorted(maintenance, key=_maint_priority)
            actual_maintenance = sorted_maint[:max_maintenance]
            logger.warning(
                "Multiple streams (%s) showed maintenance, but only %d spare available. "
                "Designating %s as maintenance stream; remaining streams assigned to slots.",
                [s.stream_id for s in maintenance],
                max_maintenance,
                [s.stream_id for s in actual_maintenance],
            )
            maintenance = actual_maintenance

        maintenance_ids = {s.stream_id for s in maintenance}

        for s in self.streams:
            if s.stream_id in maintenance_ids:
                s.mark(
                    "maintenance",
                    getattr(s, "_probe_maintenance_reason", "")
                    or "maintenance condition confirmed during startup probe",
                )

        available = [s for s in self.streams if s.stream_id not in maintenance_ids]

        # Critical invariant: every logical slot MUST be assigned a stream.
        # If available streams are fewer than needed slots (e.g. due to connection outages),
        # backfill from remaining streams so no furnace slot is left without video/monitoring.
        order = list(config.FURNACE_SLOTS.keys())          # ["5-1", "5-2"] — no more hardcoded names
        if len(available) < len(order):
            for s in self.streams:
                if s not in available:
                    available.append(s)
                if len(available) >= len(order):
                    break

        logger.info("Startup probe done. maintenance=%s available=%s",
                    list(maintenance_ids), [s.stream_id for s in available])

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

        # 1. First: does any maintenance stream now show signs of life?
        # This is the only thing that can clear a maintenance flag; after a
        # swap no slot is assigned to it, so nothing else watches it.
        self._check_maintenance_streams_for_resume()

        for slot in self.slots.values():
            sm = slot.state_machine
            if sm is None or slot.current_stream is None:
                continue

            # 2. Slot sitting on a maintenance stream: keep re-evaluating
            # until a replacement becomes available. (No change from before.)
            if slot.current_stream.status == "maintenance":
                self._trigger_reevaluation(
                    slot,
                    excluded_stream_id=slot.current_stream.stream_id,
                )
                return

            # 3. Idle slot that has lost activefire for too long. (Unchanged.)
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
                return

    def _on_slot_lost_activefire(self, slot):
        """Called by a slot's state machine when it wipes a heat for lack
        of active fire."""
        self._trigger_reevaluation(slot)

    def _on_slot_maintenance_detected(self, slot, reason):
        stream = slot.current_stream
        excluded_stream_id = None

        if stream is not None:
            excluded_stream_id = stream.stream_id

            stream.mark(
                "maintenance",
                reason,
            )

            logger.warning(
                "Slot %s stream %s entered maintenance (%s) - "
                "triggering swap",
                slot.slug,
                stream.stream_id,
                reason,
            )

        self._trigger_reevaluation(
            slot,
            excluded_stream_id=excluded_stream_id,
        )

    def _trigger_reevaluation(self, slot=None, excluded_stream_id=None):
        if self._reeval_thread and self._reeval_thread.is_alive():
            return

        # The triggering slot is only used for the thread name / logging in
        # the new priority-based re-evaluation. When called from the
        # maintenance-cleared path there is no specific slot, so borrow the
        # first one.
        triggering_slot = slot or next(iter(self.slots.values()))

        self._reeval_thread = threading.Thread(
            target=self._reevaluate_streams,
            args=(triggering_slot, excluded_stream_id),
            name=f"ReevalStreams-{triggering_slot.slug}",
            daemon=True,
        )
        self._reeval_thread.start()

    def _reevaluate_streams(self, failing_slot, excluded_stream_id=None):
        """Probe all three streams for class 5 in parallel, then reassign
        slots by strict priority.

        Priority order is the order the streams appear in config.STREAMS
        (stream-1 > stream-2 > stream-3). The highest-priority available
        stream becomes furnace 4/1, the next becomes furnace 4/2. Streams
        that show no activefire, or that are flagged 'maintenance', are not
        candidates. Any leftover stream (available but not needed) is marked
        'spare'.

        This reproduces every case in the operator spec:
          {1,2}   -> 1->4/1, 2->4/2
          {1,3}   -> 1->4/1, 3->4/2
          {2,3}   -> 2->4/1, 3->4/2
          {2,3} with 1 back -> 1->4/1, 2->4/2
          etc.
        """
        with self._lock:
            logger.info("Re-evaluating streams (triggered by %s)", failing_slot.slug)

            # ---- Parallel probe for active fire -----------------------------
            results = {}
            results_lock = threading.Lock()

            def probe_one(stream):
                # A stream already known to be under maintenance is never a
                # candidate, even if a stray class-3 frame comes through.
                if stream.status == "maintenance":
                    with results_lock:
                        results[stream.stream_id] = False
                    return
                ok = self._probe_stream(
                    stream.rtsp_url,
                    config.PROBE_ACTIVEFIRE_SECONDS,
                    target_class=CLS_FURNACE_ACTIVEFIRE,
                )
                with results_lock:
                    results[stream.stream_id] = ok

            threads = [
                threading.Thread(target=probe_one, args=(s,), daemon=True)
                for s in self.streams
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=config.PROBE_ACTIVEFIRE_SECONDS + 10)

            # ---- Pick the two highest-priority available streams -----------
            # self.streams is already in config.STREAMS order = priority order.
            available = [
                s for s in self.streams
                if results.get(s.stream_id) and s.status != "maintenance"
            ]

            logger.info(
                "Probe results: %s. Priority-ordered available: %s",
                {s.stream_id: bool(results.get(s.stream_id)) for s in self.streams},
                [s.stream_id for s in available],
            )

            slot_order = list(config.FURNACE_SLOTS.keys())   # 5/1 takes available[0], 5/2 takes available[1]

            # Critical: if fewer streams showed active fire than needed slots
            # (e.g. one furnace is firing while the other is charging scrap / tapping / idle),
            # backfill from non-maintenance streams first, then remaining streams,
            # so NO slot is ever detached and left without video or monitoring.
            if len(available) < len(slot_order):
                for s in self.streams:
                    if s not in available and s.status != "maintenance":
                        available.append(s)
                    if len(available) >= len(slot_order):
                        break

            if len(available) < len(slot_order):
                for s in self.streams:
                    if s not in available:
                        available.append(s)
                    if len(available) >= len(slot_order):
                        break

            if len(available) < len(slot_order):
                logger.warning(
                    "Only %d stream(s) available (need %d to fully staff %s). "
                    "The lowest-priority slot(s) will be left unassigned.",
                    len(available), len(slot_order), "+".join(slot_order),
                )

            assignment = {}
            for i, slug in enumerate(slot_order):
                if i < len(available):
                    assignment[slug] = available[i]

            # ---- Apply -----------------------------------------------------
            # Release any stream being vacated before attaching, so a stream
            # shifting between slots (e.g. 4/2 -> 4/1) isn't accidentally
            # cleared when the second slot releases its old binding.
            for slug in slot_order:
                slot = self.slots[slug]
                new_stream = assignment.get(slug)
                if new_stream is slot.current_stream:
                    continue
                if slot.current_stream is not None:
                    old = slot.current_stream
                    old.assigned_slot = None
                    if old.status != "maintenance":
                        old.mark("spare", f"released by {slug} during re-evaluation")
                    slot.detach()

            for slug in slot_order:
                slot = self.slots[slug]
                new_stream = assignment.get(slug)
                if new_stream is slot.current_stream:
                    continue
                if new_stream is not None:
                    slot.attach_to_stream(new_stream)
                    logger.info(
                        "Re-evaluation bound slot %s -> %s (PYRO %s)",
                        slug, new_stream.stream_id, slot.pyro_field,
                    )
                else:
                    slot.detach()
                    logger.warning("Slot %s left unassigned", slug)

            # ---- Anything not assigned becomes spare ----------------------
            assigned_ids = {s.stream_id for s in assignment.values()}
            for s in self.streams:
                if s.stream_id in assigned_ids:
                    continue
                if s.status == "maintenance":
                    continue
                if s.assigned_slot is not None:
                    # Defensive: shouldn't happen after the release loop.
                    s.assigned_slot = None
                s.mark("spare", "unused after re-evaluation")

            # Clear the swap flag on the slot that started this (kept for
            # compatibility with the old state-machine callback).
            if failing_slot._swap_in_progress:
                failing_slot.swap_done()

    def _probe_for_maintenance(self):
        """Return every stream where maintenance is confirmed.

        All three streams are probed in parallel, so the wall-clock cost is
        STREAM_PROBE_SECONDS regardless of how many streams are online.
        """
        results = {}
        results_lock = threading.Lock()

        def probe_one(stream):
            ok = self._probe_stream(
                stream.rtsp_url,
                config.STREAM_PROBE_SECONDS,
                target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
                maintenance_mode=True,
            )
            with results_lock:
                results[stream.stream_id] = ok

        threads = [
            threading.Thread(target=probe_one, args=(s,), daemon=True)
            for s in self.streams
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=config.STREAM_PROBE_SECONDS + 10)

        hits = []
        for stream in self.streams:
            res = results.get(stream.stream_id)
            if res:
                stream._probe_maintenance_reason = str(res) if isinstance(res, str) else "maintenance confirmed"
                logger.warning(
                    "Stream %s shows maintenance condition (%s)",
                    stream.stream_id,
                    stream._probe_maintenance_reason,
                )
                hits.append(stream)
        return hits

    def _open_probe_capture(self, rtsp_url, retries=3):
        """
        Try several times before deciding that a camera cannot currently
        be probed.

        This avoids changing furnace assignment because of a single temporary
        RTSP/network connection failure.
        """

        retries = max(1, int(retries))

        for attempt in range(1, retries + 1):

            cap = None

            try:
                cap = cv2.VideoCapture(
                    rtsp_url,
                    cv2.CAP_FFMPEG,
                    [
                        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                        5000,

                        cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                        3000,
                    ],
                )

            except (TypeError, cv2.error):

                cap = cv2.VideoCapture(
                    rtsp_url,
                    cv2.CAP_FFMPEG,
                )

            if cap is not None and cap.isOpened():

                try:
                    cap.set(
                        cv2.CAP_PROP_BUFFERSIZE,
                        1,
                    )
                except Exception:
                    pass

                return cap

            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

            logger.warning(
                "RTSP probe connection failed: %s "
                "(attempt %d/%d)",
                rtsp_url,
                attempt,
                retries,
            )

            if attempt < retries:
                time.sleep(2)

        return None

    def _probe_stream(
        self,
        rtsp_url,
        duration_seconds,
        target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
        maintenance_mode=False,
    ):
        from detector import FurnaceDetector

        try:
            detector = FurnaceDetector()
        except Exception:
            logger.exception(
                "Could not load model during stream probe"
            )
            return False

        cap = self._open_probe_capture(
            rtsp_url,
            retries=3,
        )

        if cap is None:
            logger.warning(
                "Probe could not open %s after reconnect attempts",
                rtsp_url,
            )
            return False

        tracked_ids = (
            MAINTENANCE_TRACKED_CLASS_IDS
            if maintenance_mode
            else {target_class}
        )

        debouncer = ClassDebouncer(
            confirm_frames=config.DEBOUNCE_FRAMES,
            class_ids=tracked_ids,
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
                    {
                        d["class_id"]
                        for d in detections
                    }
                )

                if maintenance_mode:
                    reason = maintenance_reason(
                        detections,
                        debouncer,
                        confirmed,
                    )
                    if reason is not None:
                        return reason

                elif target_class in confirmed:
                    return True

        finally:
            cap.release()

        return False

    def _probe_stream_for_process_resume(self, stream, duration_seconds):
        """Probe a maintenance-flagged stream for any PROCESS_RESUME_CLASS_IDS.

        Returns the class ID that was confirmed (int), or None. Class 5
        (activefire) is checked first and wins over any other hit - it's the
        top-priority signal that the furnace is back in operation.
        """
        from detector import FurnaceDetector

        try:
            detector = FurnaceDetector()
        except Exception:
            logger.exception(
                "Could not load model during resume probe on %s",
                stream.stream_id,
            )
            return None

        cap = self._open_probe_capture(
            stream.rtsp_url,
            retries=3,
        )

        if cap is None:
            logger.warning(
                "Resume probe could not open %s after reconnect attempts",
                stream.rtsp_url,
            )
            return None

        debouncer = ClassDebouncer(
            confirm_frames=config.DEBOUNCE_FRAMES,
            class_ids=PROCESS_RESUME_CLASS_IDS,
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

                # Top priority: an actual fire beats every other resume class.
                if CLS_FURNACE_ACTIVEFIRE in confirmed:
                    return CLS_FURNACE_ACTIVEFIRE

                hits = confirmed & PROCESS_RESUME_CLASS_IDS
                if hits:
                    return sorted(hits)[0]
        finally:
            cap.release()

        return None

    def _check_maintenance_streams_for_resume(self):
        """Every watchdog tick, probe each maintenance-flagged stream. If any
        process-resume class is confirmed, the furnace behind that stream is
        no longer under maintenance - clear the flag so it becomes a
        candidate for reassignment again.

        Probes run in parallel so one slow/unreachable stream can't stall the
        tick.
        """
        maintenance_streams = [s for s in self.streams if s.status == "maintenance"]
        if not maintenance_streams:
            return

        results = {}
        results_lock = threading.Lock()

        def probe_one(stream):
            hit = self._probe_stream_for_process_resume(
                stream, config.PROBE_ACTIVEFIRE_SECONDS,
            )
            with results_lock:
                results[stream.stream_id] = hit

        threads = [
            threading.Thread(target=probe_one, args=(s,), daemon=True)
            for s in maintenance_streams
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=config.PROBE_ACTIVEFIRE_SECONDS + 10)

        cleared = False
        for stream in maintenance_streams:
            hit = results.get(stream.stream_id)
            if hit is None:
                continue
            logger.warning(
                "Stream %s was flagged maintenance but class %d confirmed - "
                "clearing maintenance flag (process resumed).",
                stream.stream_id, hit,
            )
            stream.mark(
                "spare",
                f"maintenance cleared: class {hit} confirmed "
                "(process resumed / about to start)",
            )
            cleared = True

        # If anything cleared, kick off a re-evaluation so the newly available
        # stream can be re-slotted by the priority rule.
        if cleared:
            self._trigger_reevaluation()

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
