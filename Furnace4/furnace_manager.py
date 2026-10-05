"""
Dynamic furnace slot manager.

Owns the three physical StreamSources and the two logical FurnaceSlots
(4/1 and 4/2). At startup it probes every stream for maintenance classes
(such as furnace_operation_undermaintenance, coil_position_placednearfurnace,
furnace_operation_preheating, furma_position_placednearfurnace) and treats
the first one that shows it as the current maintenance stream; the other two back the two slots,
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

import csv
import datetime
import logging
import os
import shutil
import threading
import time

import cv2

import config
from camera_worker import (
    CameraWorker,
    MAINTENANCE_DIRECT_CLASS_IDS,
    MAINTENANCE_TRACKED_CLASS_IDS,
    maintenance_reason,
)
from debounce import ClassDebouncer
from state_machine import (
    HeatStateMachine,
    INDEX_HEADERS,
    CLS_COIL_POSITION_PLACEDNEARFURNACE,      # 0
    CLS_FURNACE_OPERATION_PREHEATING,         # 7
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,   # 28
    CLS_FURMA_POSITION_PLACEDNEARFURNACE,     # 39
    CLS_FURNACE_OPERATION_FURMAREPLACED,      # 31
    CLS_FURNACE_ACTIVEFIRE,
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

    def adopt_state_machine(self, sm, source_slot=None):
        """Adopt and rebind an in-flight or idle HeatStateMachine from another slot.
        Updates slot-specific configuration (data_dir, index_csv, furnace_id,
        heat_id_prefix, temperature_monitor, on_activefire_loss) and transfers
        on-disk artifacts (heat CSV, active state JSON, index rows) if an active
        heat is in progress.
        """
        if sm is None:
            self.state_machine = None
            return None

        if source_slot is not None and source_slot.slug == self.slug:
            self.state_machine = sm
            return sm

        with sm._lock:
            old_data_dir = sm.data_dir
            old_index_csv = sm.index_csv
            heat_id = sm.heat_id

            sm.data_dir = self.data_dir
            sm.index_csv = self.index_csv
            sm.furnace_id = self.furnace_id
            sm.heat_id_prefix = self.heat_id_prefix
            sm.temperature_monitor = self.temperature_monitor
            sm._on_activefire_loss = self._handle_activefire_loss

            is_incomplete = (
                heat_id is not None
                and getattr(sm, "state", None) != HeatStateMachine.STATE_IDLE
            )

            if not is_incomplete:
                sm.heat_number = sm._load_last_heat_number()
                self.state_machine = sm
                return sm

            # Active incomplete heat: migrate files and index entry
            os.makedirs(self.data_dir, exist_ok=True)

            if sm.heat_csv_path and os.path.exists(sm.heat_csv_path):
                new_csv_path = os.path.join(self.data_dir, os.path.basename(sm.heat_csv_path))
                if os.path.abspath(sm.heat_csv_path) != os.path.abspath(new_csv_path):
                    try:
                        shutil.copy2(sm.heat_csv_path, new_csv_path)
                        try:
                            os.remove(sm.heat_csv_path)
                        except OSError:
                            pass
                        sm.heat_csv_path = new_csv_path
                    except Exception:
                        logger.exception("Failed copying heat CSV to %s", new_csv_path)

            old_active = os.path.join(old_data_dir, f".{heat_id}.active.json")
            new_active = os.path.join(self.data_dir, f".{heat_id}.active.json")
            if os.path.exists(old_active) and os.path.abspath(old_active) != os.path.abspath(new_active):
                try:
                    shutil.copy2(old_active, new_active)
                    try:
                        os.remove(old_active)
                    except OSError:
                        pass
                except Exception:
                    logger.exception("Failed copying active state file to %s", new_active)

            # Move index row
            transfer_row = None
            if old_index_csv and os.path.exists(old_index_csv):
                try:
                    kept_rows = []
                    with open(old_index_csv, "r", newline="") as f:
                        for r in csv.DictReader(f):
                            if r.get("heat_id") == heat_id:
                                transfer_row = dict(r)
                            else:
                                kept_rows.append(r)
                    with open(old_index_csv, "w", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=INDEX_HEADERS)
                        w.writeheader()
                        for r in kept_rows:
                            w.writerow({k: r.get(k, "") for k in INDEX_HEADERS})
                except Exception:
                    logger.exception("Failed removing %s from %s", heat_id, old_index_csv)

            if not os.path.exists(self.index_csv) or os.path.getsize(self.index_csv) == 0:
                try:
                    with open(self.index_csv, "w", newline="") as f:
                        csv.writer(f).writerow(INDEX_HEADERS)
                except Exception:
                    pass

            if transfer_row is None:
                transfer_row = {
                    "heat_id": heat_id,
                    "start_time": sm.heat_start.isoformat() if sm.heat_start else "",
                    "end_time": "",
                    "duration_minutes": "",
                    "tilt_count": str(sm.tilt_count),
                    "status": "IN_PROGRESS",
                    "csv_file": os.path.basename(sm.heat_csv_path) if sm.heat_csv_path else f"{heat_id}.csv",
                    "last_updated": datetime.datetime.now().isoformat(timespec="seconds"),
                }
            else:
                transfer_row["last_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
                if sm.heat_csv_path:
                    transfer_row["csv_file"] = os.path.basename(sm.heat_csv_path)

            try:
                target_rows = []
                found = False
                if os.path.exists(self.index_csv):
                    with open(self.index_csv, "r", newline="") as f:
                        for r in csv.DictReader(f):
                            if r.get("heat_id") == heat_id:
                                target_rows.append(transfer_row)
                                found = True
                            else:
                                target_rows.append(r)
                if not found:
                    target_rows.append(transfer_row)

                with open(self.index_csv, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=INDEX_HEADERS)
                    w.writeheader()
                    for r in target_rows:
                        w.writerow({k: r.get(k, "") for k in INDEX_HEADERS})
            except Exception:
                logger.exception("Failed writing %s to %s", heat_id, self.index_csv)

            try:
                sm._save_active_state()
            except Exception:
                logger.exception("Failed saving active state for %s", heat_id)

            logger.info(
                "Process %s (state=%s) transferred from slot %s to slot %s",
                heat_id, sm.state,
                getattr(source_slot, "slug", "unknown"),
                self.slug,
            )
            self.state_machine = sm
            return sm

    def attach_to_stream(self, stream):
        """(Re)bind the slot to a stream.
        If a state machine is already running for this slot (with a heat in
        progress), it is PRESERVED across stream swaps so mid-process RTSP
        changes never break an ongoing heat. Only the CameraWorker is
        replaced and pointed to the new RTSP URL. The PYRO monitor is NOT touched — it stays with the slot.
        """
        # Detach existing stream and stop its camera worker, but keep state machine alive
        self.detach(keep_state_machine=True)

        self.current_stream = stream
        stream.assigned_slot = self.slug
        stream.mark("active", f"assigned to {self.slug}")

        if self.state_machine is None:
            self.state_machine = HeatStateMachine(
                data_dir=self.data_dir,
                index_csv=self.index_csv,
                recorder=self.recorder,
                furnace_id=self.furnace_id,
                heat_id_prefix=self.heat_id_prefix,
                temperature_monitor=self.temperature_monitor,   # slot's own monitor
                on_activefire_loss=self._handle_activefire_loss,
            )
        else:
            # Re-attaching to a stream: suspend activefire watchdog until new camera connects
            self.state_machine.note_camera_state(False)

        self.camera_worker = CameraWorker(
            state_machine=self.state_machine,
            rtsp_url=stream.rtsp_url,
            stream_id=stream.stream_id,
            on_maintenance_detected=self._handle_maintenance_detected,
        )
        self.camera_worker.start()
        logger.info(
            "Slot %s bound to stream %s (PYRO %s, heat=%s, state=%s)",
            self.slug, stream.stream_id, self.pyro_field,
            getattr(self.state_machine, "heat_id", None),
            getattr(self.state_machine, "state", None),
        )

    def detach(self, keep_state_machine=False):
        """Stop the camera worker and unbind current stream.
        keep_state_machine: when True (e.g. during stream swap or re-evaluation),
        preserves the in-flight HeatStateMachine so the ongoing heat continues.
        """
        if self.camera_worker is not None:
            try:
                self.camera_worker.stop()
            except Exception:
                logger.exception("Failed to stop camera worker for %s", self.slug)
            self.camera_worker = None
        if self.current_stream is not None:
            self.current_stream.assigned_slot = None
            self.current_stream = None

        if not keep_state_machine:
            self.state_machine = None
        elif self.state_machine is not None:
            # Suspend active-fire watchdog while camera feed is detached
            self.state_machine.note_camera_state(False)

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
        if (self._swap_in_progress or self._on_maintenance_detected is None):
            return

        self._swap_in_progress = True

        try:
            self._on_maintenance_detected(self, reason)
        except Exception:
            # Never leave the flag set if the swap could not even start -
            # otherwise this slot would silently stop reacting to
            # maintenance for the rest of the process lifetime.
            self._swap_in_progress = False
            logger.exception(
                "Maintenance handler failed for slot %s - releasing swap flag",
                self.slug,
            )

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
        self._last_cold_watch_mono = 0.0

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
                s.mark(
                    "maintenance",
                    "maintenance condition confirmed during startup probe",
                )

        available = [s for s in self.streams if s.stream_id not in maintenance_ids]
        logger.info("Startup probe done. maintenance=%s available=%s",
                    list(maintenance_ids), [s.stream_id for s in available])

        order = list(self.slots.keys())
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
        last_activefire_at. Also periodically probe spare streams to see if
        one has warmed up enough to replace a cold assigned stream."""
        while not self._poll_stop.wait(config.SLOT_POLL_SECONDS):
            try:
                self._watchdog_tick()
                self._check_cold_streams_for_hot_spares()
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

    def _check_cold_streams_for_hot_spares(self):
        """If a slot is holding a cold stream (no active fire for
        COLD_STREAM_MIN_COLD_MINUTES) but an unassigned spare now shows
        active fire, kick off a re-evaluation so the hot stream can be
        swapped in.

        Only *unassigned* spares are probed — probing a stream that is
        already being read by a slot would open a second RTSP connection
        to the same camera.
        """
        now = datetime.datetime.now()

        cold_slots = []
        for slot in self.slots.values():
            sm = slot.state_machine
            stream = slot.current_stream
            if sm is None or stream is None:
                continue
            if stream.status == "maintenance":
                continue
            last = sm.last_activefire_at
            if last is None:
                continue
            cold_minutes = (now - last).total_seconds() / 60.0
            if cold_minutes >= config.COLD_STREAM_MIN_COLD_MINUTES:
                cold_slots.append(slot)

        if not cold_slots:
            return

        spares = [
            s for s in self.streams
            if s.assigned_slot is None and s.status != "maintenance"
        ]
        if not spares:
            return

        # Throttle. Probing a spare briefly opens an RTSP connection, so
        # keep this clearly larger than PROBE_ACTIVEFIRE_SECONDS.
        now_mono = time.monotonic()
        if now_mono - getattr(self, "_last_cold_watch_mono", 0.0) < config.COLD_STREAM_WATCH_SECONDS:
            return
        self._last_cold_watch_mono = now_mono

        for spare in spares:
            try:
                if self._probe_stream(
                    spare.rtsp_url,
                    config.PROBE_ACTIVEFIRE_SECONDS,
                    target_class=CLS_FURNACE_ACTIVEFIRE,
                    label=f"{spare.stream_id} ({spare.name})",
                ):
                    logger.info(
                        "Cold slot %s (stream %s) has had no active fire for "
                        "%.1f min, but spare %s now shows active fire — "
                        "re-evaluating to swap it in.",
                        cold_slots[0].slug,
                        cold_slots[0].current_stream.stream_id,
                        (now - cold_slots[0].state_machine.last_activefire_at)
                            .total_seconds() / 60.0,
                        spare.stream_id,
                    )
                    self._trigger_reevaluation(cold_slots[0])
                    return
            except Exception:
                logger.exception("Cold-stream probe failed for %s", spare.stream_id)

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
            # A re-evaluation is already in flight; it will look at every
            # stream anyway. Release the caller's swap flag so the slot can
            # re-trigger on the next watchdog tick if it is still in
            # maintenance (instead of being permanently stuck).
            if slot is not None and slot._swap_in_progress:
                slot.swap_done()
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

    def _plan_process_shifts(self, old_slot_state, assignment, slot_order):
        """Determine how state machines / processes shift across slots during re-evaluation.

        Specification / Rule:
        1. When a stream moves from slot_src to slot_dest (e.g. rtsp2 is allocated
           to slot1 and moves to slot2), the current process of slot_src must get
           shifted to slot_dest with that rtsp.
        2. When a stream exits maintenance and is assigned to slot_src (e.g. rtsp1
           gets out of maintenance and is assigned to slot1), and a 3rd stream
           (e.g. rtsp3 on slot2) is displaced:
           - The current process of the 3rd rtsp (from slot2) must get shifted to slot1
             IF NOT COMPLETED.
           - If it is completed (IDLE), slot1 receives a fresh state machine.
        3. If both streams swap slots (e.g. s1 -> slot2, s2 -> slot1), each slot
           receives the process of the stream that moved into it.
        4. If a slot's stream assignment is unchanged, it keeps its current process.
        """
        plan = {}
        if len(slot_order) < 2:
            for slug in slot_order:
                plan[slug] = ("keep", old_slot_state[slug]["state_machine"], self.slots[slug])
            return plan

        s1_slug, s2_slug = slot_order[0], slot_order[1]
        old_s1 = old_slot_state[s1_slug]["stream"]
        old_s2 = old_slot_state[s2_slug]["stream"]
        sm1 = old_slot_state[s1_slug]["state_machine"]
        sm2 = old_slot_state[s2_slug]["state_machine"]

        new_s1 = assignment.get(s1_slug)
        new_s2 = assignment.get(s2_slug)

        def is_incomplete(sm):
            return (
                sm is not None
                and getattr(sm, "state", None) != HeatStateMachine.STATE_IDLE
                and getattr(sm, "heat_id", None) is not None
            )

        # Check if stream from s1 moved to s2
        s1_to_s2 = (old_s1 is not None and new_s2 is not None and old_s1.stream_id == new_s2.stream_id)
        # Check if stream from s2 moved to s1
        s2_to_s1 = (old_s2 is not None and new_s1 is not None and old_s2.stream_id == new_s1.stream_id)

        if s1_to_s2 and s2_to_s1:
            # Both streams swapped slots: processes follow their streams
            plan[s1_slug] = ("transfer", sm2, self.slots[s2_slug])
            plan[s2_slug] = ("transfer", sm1, self.slots[s1_slug])

        elif s1_to_s2:
            # Stream on slot1 (e.g. rtsp2) moved to slot2:
            # Current process of slot1 must get shifted to slot2 with the rtsp
            plan[s2_slug] = ("transfer", sm1, self.slots[s1_slug])

            # Displaced stream on slot2 (e.g. rtsp3):
            # Current process of 3rd rtsp must get shifted to slot1 (if not completed)
            if is_incomplete(sm2):
                plan[s1_slug] = ("transfer", sm2, self.slots[s2_slug])
            else:
                plan[s1_slug] = ("fresh", None, None)

        elif s2_to_s1:
            # Stream on slot2 moved to slot1:
            plan[s1_slug] = ("transfer", sm2, self.slots[s2_slug])

            # Displaced stream on slot1:
            if is_incomplete(sm1):
                plan[s2_slug] = ("transfer", sm1, self.slots[s1_slug])
            else:
                plan[s2_slug] = ("fresh", None, None)

        else:
            for slug in slot_order:
                old_s = old_slot_state[slug]["stream"]
                new_s = assignment.get(slug)
                curr_sm = old_slot_state[slug]["state_machine"]
                if old_s is not None and new_s is not None and old_s.stream_id == new_s.stream_id:
                    plan[slug] = ("keep", curr_sm, self.slots[slug])
                else:
                    plan[slug] = ("keep", curr_sm, self.slots[slug])

        return plan

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
        try:
            with self._lock:
                logger.info(
                    "Re-evaluating streams (triggered by %s, excluded=%s)",
                    failing_slot.slug,
                    excluded_stream_id,
                )

                # ---- Parallel probe for active fire -----------------------------
                results = {}
                results_lock = threading.Lock()

                def probe_one(stream):
                    # A stream already known to be under maintenance or excluded is never a candidate.
                    if stream.status == "maintenance" or stream.stream_id == excluded_stream_id:
                        with results_lock:
                            results[stream.stream_id] = False
                        return

                    # If this stream is already assigned to a healthy running slot (and not failing),
                    # inspect the existing live worker instead of opening a conflicting RTSP stream.
                    active_slot = None
                    for sl in self.slots.values():
                        if sl.current_stream is stream and sl is not failing_slot:
                            active_slot = sl
                            break

                    if active_slot is not None and active_slot.camera_worker is not None:
                        cw = active_slot.camera_worker
                        try:
                            is_active = (
                                cw.is_connected()
                                and cw.debouncer.is_confirmed(CLS_FURNACE_ACTIVEFIRE)
                            )
                        except Exception:
                            is_active = False
                        with results_lock:
                            results[stream.stream_id] = is_active
                        logger.info(
                            "Probe (reused live worker): %s -> %s",
                            stream.stream_id, "fire" if is_active else "no fire",
                        )
                        return

                    ok = self._probe_stream(
                        stream.rtsp_url,
                        config.ALLOCATION_ACTIVEFIRE_PROBE_SECONDS,      # <- 30s
                        target_class=CLS_FURNACE_ACTIVEFIRE,
                        label=f"{stream.stream_id} ({stream.name})",
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
                    t.join(timeout=config.ALLOCATION_ACTIVEFIRE_PROBE_SECONDS + 15)

                # Candidate streams are those not in maintenance and not excluded
                candidate_streams = [
                    s for s in self.streams
                    if s.status != "maintenance" and s.stream_id != excluded_stream_id
                ]

                # --- Split by active-fire presence (the primary signal) ---------------
                fire_streams = [s for s in candidate_streams if results.get(s.stream_id)]
                cold_streams = [s for s in candidate_streams if not results.get(s.stream_id)]

                logger.info(
                    "Probe results: fire=%s cold=%s",
                    [s.stream_id for s in fire_streams],
                    [s.stream_id for s in cold_streams],
                )

                slot_order = list(self.slots.keys())    # 4-1 first, then 4-2
                assignment = {}

                if len(fire_streams) >= 2:
                    # 2 (or 3) cameras see active fire: the two highest-priority
                    # fire streams get the two slots. Priority = config.STREAMS order.
                    # (Same behaviour as before — this is the "happy path".)
                    for i, slug in enumerate(slot_order):
                        if i < len(fire_streams):
                            assignment[slug] = fire_streams[i]
                        elif (i - len(fire_streams)) < len(cold_streams):
                            assignment[slug] = cold_streams[i - len(fire_streams)]

                elif len(fire_streams) == 1:
                    # Exactly one camera sees active fire. Give that stream to the
                    # "desired" slot (the failing slot if we know it, else slot
                    # priority 1), and put the best cold stream on the other slot so
                    # that slot is not left with a blank feed.
                    #
                    # The watchdog will keep probing the cold stream
                    # (_check_cold_streams_for_hot_spares below); if it warms up, a
                    # second re-evaluation swaps it in.
                    fire_stream = fire_streams[0]

                    if failing_slot is not None and failing_slot.slug in slot_order:
                        desired_slug = failing_slot.slug
                    else:
                        desired_slug = slot_order[0]
                    other_slug = next(s for s in slot_order if s != desired_slug)

                    assignment[desired_slug] = fire_stream
                    if cold_streams:
                        assignment[other_slug] = cold_streams[0]

                    logger.info(
                        "Only 1 fire stream (%s). Assigning to slot %s; cold %s -> slot %s. "
                        "Watchdog will swap if the cold stream becomes hot.",
                        fire_stream.stream_id, desired_slug,
                        cold_streams[0].stream_id if cold_streams else "none",
                        other_slug,
                    )

                else:
                    # No camera currently shows active fire. Fall back to priority
                    # order over the cold streams so both slots are still staffed.
                    for i, slug in enumerate(slot_order):
                        if i < len(cold_streams):
                            assignment[slug] = cold_streams[i]

                # ---- Apply -----------------------------------------------------
                # 1. Snapshot old slot state before detaching
                old_slot_state = {
                    slug: {
                        "stream": self.slots[slug].current_stream,
                        "state_machine": self.slots[slug].state_machine,
                    }
                    for slug in slot_order
                }
                process_plan = self._plan_process_shifts(old_slot_state, assignment, slot_order)

                # 2. Release any stream being vacated before attaching, so a stream
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
                        slot.detach(keep_state_machine=True)

                # 3. Apply process / state machine shifts across slots
                for slug in slot_order:
                    slot = self.slots[slug]
                    action, source_sm, src_slot = process_plan[slug]
                    if action == "transfer":
                        slot.adopt_state_machine(source_sm, source_slot=src_slot)
                    elif action == "fresh":
                        slot.state_machine = None
                    elif action == "keep":
                        pass

                # 4. Attach new streams to slots
                for slug in slot_order:
                    slot = self.slots[slug]
                    new_stream = assignment.get(slug)
                    if new_stream is slot.current_stream and slot.camera_worker is not None:
                        continue
                    if new_stream is not None:
                        slot.attach_to_stream(new_stream)
                        logger.info(
                            "Re-evaluation bound slot %s -> %s (PYRO %s)",
                            slug, new_stream.stream_id, slot.pyro_field,
                        )
                    else:
                        slot.detach(keep_state_machine=True)
                        logger.warning("Slot %s left unassigned (in-progress heat preserved)", slug)

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
        finally:
            # Guarantee swap flags are cleared on all slots so future swaps can trigger
            for s in self.slots.values():
                if s._swap_in_progress:
                    s.swap_done()

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
                label=f"{stream.stream_id} ({stream.name})",
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
            if results.get(stream.stream_id):
                logger.warning(
                    "Stream %s shows maintenance condition", stream.stream_id,
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
                )
            except Exception:
                cap = None

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
        label=None,
    ):
        """Probe one stream for a target class.

        label: optional short string used in the countdown log so an operator
        watching the console can tell which probe is which. When omitted the
        RTSP URL is used.
        """
        from detector import FurnaceDetector

        label = label or rtsp_url

        try:
            detector = FurnaceDetector()
        except Exception:
            logger.exception("Could not load model during stream probe")
            return False

        cap = self._open_probe_capture(rtsp_url, retries=3)
        if cap is None:
            logger.warning(
                "Probe could not open %s after reconnect attempts", rtsp_url,
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
        next_tick = time.monotonic() + config.ALLOCATION_PROBE_TICK_SECONDS

        logger.info(
            "Probe start: %s (target=%s, window=%.0fs)",
            label, target_class, duration_seconds,
        )

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

                if maintenance_mode:
                    if maintenance_reason(detections, debouncer, confirmed) is not None:
                        remaining = max(0, deadline - time.monotonic())
                        logger.info(
                            "Probe hit: %s (maintenance) after %.1fs, %.1fs left",
                            label, duration_seconds - remaining, remaining,
                        )
                        return True
                elif target_class in confirmed:
                    remaining = max(0, deadline - time.monotonic())
                    logger.info(
                        "Probe hit: %s (class %d) after %.1fs, %.1fs left",
                        label, target_class, duration_seconds - remaining, remaining,
                    )
                    return True

                # Countdown tick — purely informational.
                now = time.monotonic()
                if now >= next_tick:
                    remaining = max(0, deadline - now)
                    logger.info(
                        "Probe … %s: %.0fs remaining (class %d not yet confirmed)",
                        label, remaining, target_class,
                    )
                    next_tick = now + config.ALLOCATION_PROBE_TICK_SECONDS

        finally:
            cap.release()

        logger.info("Probe miss: %s (no class %d within %.0fs)",
                    label, target_class, duration_seconds)
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
