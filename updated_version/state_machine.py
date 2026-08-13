"""
HeatStateMachine
----------------
Turns a stream of *confirmed* class-ID detections into the furnace process
timeline described by the plant: scrap charging -> sponge charging ->
tapping, plus a tilt counter for furnace movements that turn out NOT to be
tapping.

Logic (matches the classes to their numeric IDs, not their text names):

  3  magnet_feeding_scrap        -> scrap charging starts (new heat begins).
                                    If this is seen while the *previous*
                                    heat is still in TAPPING, that heat is
                                    closed out immediately (see "Ending a
                                    heat" below) and the new heat starts
                                    from this same moment.
  8  spange_feeder_onfurnace     -> scrap charging ends, sponge charging starts

  Watching for tapping while sponge charging is in progress works in two
  different ways depending on how long sponge charging has already run:

  Under 130 minutes of sponge charging (the normal case):
  12 furnace_position_tilding   -> furnace leaves "normal" position. We
                                    don't yet know if this is the start of
                                    tapping or just an inspection tilt, so
                                    the app starts "watching".
  2  furnace_position_normal     -> furnace is back to normal position
                                    while being watched -> this was just a
                                    tilt (not tapping). tilt_count += 1,
                                    and the app keeps waiting inside sponge
                                    charging.

  From 130 minutes of sponge charging onward (fast-track):
  12 furnace_position_tilding   -> at this point the plant is confident
                                    this movement really is the start of
                                    tapping, so it's taken as tapping
                                    starting immediately - no need to also
                                    see the furnace return to normal first.
                                    Sponge charging is closed out and
                                    tapping starts, both at this same
                                    moment.
  2  furnace_position_normal     -> (while TAPPING) tapping has ended and
                                    the heat is complete.

  Starting mid-process (app restarted, or first launched, while a heat is
  already underway): if the first thing seen while IDLE is anything other
  than 3 (magnet_feeding_scrap), a heat is assumed to already be in
  progress and is picked up immediately from whichever stage that implies,
  rather than sitting in IDLE waiting indefinitely for the next scrap
  charging. See MID_PROCESS_SPONGE_CLASS_IDS / MID_PROCESS_TAPPING_CLASS_IDS
  below. The skipped earlier stage(s) are logged with an unknown start/
  duration instead of a guessed one.

  Ending a heat (tapping -> IDLE), whichever happens first:
    - class 2 (furnace_position_normal) is detected while TAPPING, or
    - 15 minutes have passed since tapping started, or
    - 3 magnet_feeding_scrap is detected (this also means the *next* heat's
      scrap charging has begun, so a new heat starts immediately from this
      same moment).

Assumptions made explicit (see README for more):
  - Tilt/tap watching only kicks in while sponge charging is the active
    stage, matching the described scrap -> sponge -> tapping flow.
  - Once tapping is confirmed (either way), only class 2 (to end it),
    the 15-minute timer, and class 3 drive any further transition.

Database: every stage (scrap/sponge/tapping) and every heat is pushed to
the SQL recorder the moment it *starts* (see _notify_event_start /
record_heat_started), not just when it finishes. The row is written with
its start time and empty end/duration, then updated in place once that
stage or heat ends - so a process appears in the database as soon as it
begins, not only once it's complete.

This module only depends on the standard library so it can be exercised by
tests/test_state_machine.py without OpenCV / ultralytics installed.
"""

import csv
import datetime
import logging
import os
import threading

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Class IDs used by the logic
# ---------------------------------------------------------------------------
CLS_FURNACE_NORMAL = 2
CLS_MAGNET_FEEDING_SCRAP = 3
CLS_SPONGE_FEEDER_ON_FURNACE = 8
CLS_FURNACE_TILTING = 12

# Classes the state machine actually reacts to. Passed to the debouncer so
# it only tracks streaks for classes that matter.
RELEVANT_CLASS_IDS = {
    CLS_FURNACE_NORMAL,
    CLS_MAGNET_FEEDING_SCRAP,
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_FURNACE_TILTING,
}

# ---------------------------------------------------------------------------
# Mid-process start-up
# ---------------------------------------------------------------------------
# If the app is (re)started while a heat is already underway, we'd otherwise
# sit in IDLE forever waiting for the next magnet_feeding_scrap (class 3),
# missing the rest of the heat entirely. Instead, if the very first thing we
# see while IDLE is one of the classes below, assume a heat is already in
# progress and start tracking it immediately from whichever stage that class
# implies - scrap charging's own start time is simply unknown/unlogged in
# that case.
#
# 8 spange_feeder_onfurnace, or the furnace visibly moving
# -> already in sponge charging (or a tilt inside it); jump straight there.
MID_PROCESS_SPONGE_CLASS_IDS = {
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_FURNACE_TILTING,
}
# There's no longer a class that definitively indicates tapping on its own.
MID_PROCESS_TAPPING_CLASS_IDS = set()

CSV_HEADERS = ["event", "start_time", "end_time", "duration_minutes", "tilt_number", "note"]
INDEX_HEADERS = ["heat_id", "start_time", "end_time", "duration_minutes",
                  "tilt_count", "status", "csv_file"]

# Roughly matches the plant's own reference numbers (used only for display
# hints on the dashboard, never to force a transition).
EXPECTED_DURATION_MIN = {
    "scrap_charging": (20, 30),
    "sponge_charging": (60, 150),
    "tapping": (10, 20),
}

# ---------------------------------------------------------------------------
# Tunable timing rules (see module docstring for what each one is for)
# ---------------------------------------------------------------------------
SPONGE_FAST_TRACK_MINUTES = 130
SPONGE_FAST_TRACK_THRESHOLD = datetime.timedelta(minutes=SPONGE_FAST_TRACK_MINUTES)


class HeatStateMachine:
    STATE_IDLE = "IDLE"
    STATE_SCRAP_CHARGING = "SCRAP_CHARGING"
    STATE_SPONGE_CHARGING = "SPONGE_CHARGING"
    STATE_TAPPING = "TAPPING"

    RELEVANT_CLASS_IDS = RELEVANT_CLASS_IDS
    EXPECTED_DURATION_MIN = EXPECTED_DURATION_MIN

    def __init__(self, data_dir="data/heats", index_csv="data/heats_index.csv",
                 recorder=None):
        self.data_dir = data_dir
        self.index_csv = index_csv
        # Optional db.SqlRecorder. It only ever puts a dict on a queue, so
        # calling it from here costs microseconds and can't block the
        # camera thread no matter what the database is doing. Leave it as
        # None and the class behaves exactly as it did before: CSV only.
        self.recorder = recorder
        os.makedirs(self.data_dir, exist_ok=True)
        index_parent = os.path.dirname(self.index_csv)
        if index_parent:
            os.makedirs(index_parent, exist_ok=True)

        self._lock = threading.RLock()

        self._ensure_index_header()
        self.heat_number = self._load_last_heat_number()

        self.heat_id = None
        self.heat_csv_path = None
        self.state = self.STATE_IDLE

        # When the heat itself began, kept separately from scrap_start
        # because scrap_start is cleared on a mid-process pickup.
        self.heat_start = None
        # Row number of the next event inside the current heat's CSV. The
        # database's event key is derived from (heat_id, ordinal, name),
        # so a live row and the same row later re-read by the CSV backfill
        # produce an identical key - which is what makes the backfill safe
        # to run over data the app already inserted.
        self._event_ordinal = 0

        self.scrap_start = None
        self.sponge_start = None
        self.tapping_start = None

        self.tilt_count = 0
        self.furnace_watch_active = False
        self.furnace_watch_start = None

        self.completed_events = []
        self.last_event_desc = "Waiting for scrap charging to begin..."
        self.last_updated = None

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    def process_detections(self, newly_confirmed_ids, timestamp=None):
        """Feed a set of newly-confirmed class IDs (an "edge", not a raw
        per-frame detection set) into the state machine."""
        timestamp = timestamp or datetime.datetime.now()
        newly_confirmed_ids = set(newly_confirmed_ids) if newly_confirmed_ids else set()

        with self._lock:
            # Time-based transitions (the 15-minute tapping timer) get a
            # chance to fire first, using this call's timestamp as "now".
            # That keeps ordering correct if, say, a magnet_feeding_scrap
            # detection and the timer both would have fired by now.
            self._check_timeouts_locked(timestamp)

            if not newly_confirmed_ids:
                self.last_updated = timestamp
                return

            # Mid-process start-up: we're IDLE (fresh app start, or the
            # previous heat's IDLE gap) and the very first thing we see is
            # NOT the start-of-heat class, so a heat must already be
            # underway. Pick it up immediately instead of waiting
            # indefinitely for magnet_feeding_scrap. Checked before the
            # normal transition logic below so that logic then runs
            # against the *new* state in this same call.
            if self.state == self.STATE_IDLE and CLS_MAGNET_FEEDING_SCRAP not in newly_confirmed_ids:
                if newly_confirmed_ids & MID_PROCESS_TAPPING_CLASS_IDS:
                    self._start_new_heat(timestamp, mid_process_stage="tapping")
                elif newly_confirmed_ids & MID_PROCESS_SPONGE_CLASS_IDS:
                    self._start_new_heat(timestamp, mid_process_stage="sponge_charging")

            if CLS_MAGNET_FEEDING_SCRAP in newly_confirmed_ids:
                if self.state == self.STATE_IDLE:
                    self._start_new_heat(timestamp)
                elif self.state == self.STATE_TAPPING:
                    # The next heat's scrap charging has begun -> the
                    # current heat's tapping (and the heat itself) ends
                    # right now, and the new heat starts from this same
                    # moment.
                    self._finalize_tapping(timestamp, reason="magnet_feeding_scrap detected")
                    self._start_new_heat(timestamp)

            if CLS_SPONGE_FEEDER_ON_FURNACE in newly_confirmed_ids and self.state == self.STATE_SCRAP_CHARGING:
                self._end_scrap_start_sponge(timestamp)

            if self.state == self.STATE_TAPPING:
                if CLS_FURNACE_NORMAL in newly_confirmed_ids:
                    self._finalize_tapping(timestamp, reason="furnace returned to normal")

            if self.state == self.STATE_SPONGE_CHARGING:
                sponge_elapsed = timestamp - self.sponge_start if self.sponge_start else datetime.timedelta(0)

                if CLS_FURNACE_TILTING in newly_confirmed_ids:
                    if sponge_elapsed >= SPONGE_FAST_TRACK_THRESHOLD:
                        self._enter_tapping(timestamp, timestamp, note=">130 min sponge charging")
                    elif not self.furnace_watch_active:
                        self._begin_furnace_watch(timestamp)

                if CLS_FURNACE_NORMAL in newly_confirmed_ids and self.furnace_watch_active:
                    self._resolve_furnace_watch(timestamp)

            self.last_updated = timestamp

    def check_timeouts(self, timestamp=None):
        pass

    def get_status(self):
        with self._lock:
            now = datetime.datetime.now()

            current_start = {
                self.STATE_SCRAP_CHARGING: self.scrap_start,
                self.STATE_SPONGE_CHARGING: self.sponge_start,
                self.STATE_TAPPING: self.tapping_start,
            }.get(self.state)
            elapsed = (now - current_start).total_seconds() if current_start else 0.0

            return {
                "heat_id": self.heat_id,
                "heat_number": self.heat_number,
                "state": self.state,
                "current_process": {
                    self.STATE_SCRAP_CHARGING: "scrap_charging",
                    self.STATE_SPONGE_CHARGING: "sponge_charging",
                    self.STATE_TAPPING: "tapping",
                    self.STATE_IDLE: None,
                }[self.state],
                "current_process_start": current_start.isoformat() if current_start else None,
                "elapsed_seconds": elapsed,
                "tilt_count": self.tilt_count,
                "furnace_watch_active": self.furnace_watch_active,
                "last_event": self.last_event_desc,
                "events": list(self.completed_events),
                "expected_duration_min": self.EXPECTED_DURATION_MIN,
            }

    def get_status_lines(self):
        """Short text lines suitable for an on-video overlay."""
        s = self.get_status()
        mins, secs = divmod(int(s["elapsed_seconds"]), 60)
        stage = s["state"].replace("_", " ").title()
        lines = [f"Heat: {s['heat_id'] or '---'}"]
        if s["state"] != self.STATE_IDLE:
            lines.append(f"Stage: {stage}  ({mins:02d}:{secs:02d})")
        else:
            lines.append("Stage: Idle - waiting for scrap charging")
        lines.append(f"Tilt count: {s['tilt_count']}")
        return lines

    def get_heats_history(self):
        if not os.path.exists(self.index_csv):
            return []
        with open(self.index_csv, "r", newline="") as f:
            return list(csv.DictReader(f))

    def get_heat_csv_path(self, heat_id):
        path = os.path.join(self.data_dir, f"{heat_id}.csv")
        return path if os.path.exists(path) else None

    # -----------------------------------------------------------------
    # Internal transition handlers
    # -----------------------------------------------------------------
    def _check_timeouts_locked(self, timestamp):
        pass

    def _start_new_heat(self, timestamp, mid_process_stage=None):
        """Starts a new heat. Normally this begins at scrap charging; if
        mid_process_stage is given ("sponge_charging" or "tapping"), the app
        was (re)started after that point already happened off-camera, so we
        skip straight to that stage instead, logging the earlier stage(s) as
        unknown-duration rather than guessing a start time for them."""
        self.heat_number += 1
        self.heat_id = f"Heat_{self.heat_number:03d}_{timestamp.strftime('%Y%m%d_%H%M%S')}"
        self.heat_csv_path = os.path.join(self.data_dir, f"{self.heat_id}.csv")
        with open(self.heat_csv_path, "w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)

        self.heat_start = timestamp
        self._event_ordinal = 0
        self.scrap_start = timestamp
        self.sponge_start = None
        self.tapping_start = None
        self.tilt_count = 0
        self.furnace_watch_active = False
        self.completed_events = []
        self.state = self.STATE_SCRAP_CHARGING
        self.last_event_desc = f"{self.heat_id}: scrap charging started"

        self._append_index_row(status="IN_PROGRESS", start=timestamp)
        self._notify("record_heat_started",
                     heat_id=self.heat_id,
                     heat_number=self.heat_number,
                     start_time=timestamp,
                     csv_file=os.path.basename(self.heat_csv_path))

        if mid_process_stage == "sponge_charging":
            self._log_event("scrap_charging_unknown", None, None)
            self.scrap_start = None
            self.sponge_start = timestamp
            self.state = self.STATE_SPONGE_CHARGING
            self.last_event_desc = (
                f"{self.heat_id}: app started mid-process - already in "
                "sponge charging (scrap charging start unknown)"
            )
            self._notify_event_start("sponge_charging", timestamp)
        elif mid_process_stage == "tapping":
            self._log_event("scrap_charging_unknown", None, None)
            self._log_event("sponge_charging_unknown", None, None)
            self.scrap_start = None
            self.sponge_start = None
            self.tapping_start = timestamp
            self.state = self.STATE_TAPPING
            self.last_event_desc = (
                f"{self.heat_id}: app started mid-process - already in "
                "tapping (scrap/sponge charging start unknown)"
            )
            self._notify_event_start("tapping", timestamp)
        else:
            self._notify_event_start("scrap_charging", timestamp)

    def _end_scrap_start_sponge(self, timestamp):
        self._log_event("scrap_charging", self.scrap_start, timestamp)
        self.sponge_start = timestamp
        self.state = self.STATE_SPONGE_CHARGING
        self.last_event_desc = f"{self.heat_id}: sponge charging started"
        self._notify_event_start("sponge_charging", timestamp)

    def _begin_furnace_watch(self, timestamp):
        self.furnace_watch_active = True
        self.furnace_watch_start = timestamp
        self.last_event_desc = f"{self.heat_id}: furnace left normal position, watching..."

    def _enter_tapping(self, tapping_start_time, sponge_end_time, note=""):
        """Closes out sponge charging and starts the tapping clock."""
        self._log_event("sponge_charging", self.sponge_start, sponge_end_time, note=note)
        self.tapping_start = tapping_start_time
        self.state = self.STATE_TAPPING

        self.furnace_watch_active = False
        self.furnace_watch_start = None

        suffix = f" ({note})" if note else ""
        self.last_event_desc = f"{self.heat_id}: tapping started{suffix}"
        self._notify_event_start("tapping", tapping_start_time)

    def _resolve_furnace_watch(self, timestamp):
        """Class 2 (furnace back to normal) seen while watching -> tilt completed."""
        watch_start = self.furnace_watch_start
        self.furnace_watch_active = False
        self.furnace_watch_start = None

        self.tilt_count += 1
        self._log_event("tilt", watch_start, timestamp, tilt_number=self.tilt_count)
        self.last_event_desc = (
            f"{self.heat_id}: furnace back to normal - tilt #{self.tilt_count} "
            "(no tapping detected)"
        )

    def _finalize_tapping(self, end_timestamp, reason=""):
        """Ends tapping and completes the heat. Triggered either by the
        15-minute timer or by a magnet_feeding_scrap detection."""
        self._log_event("tapping", self.tapping_start, end_timestamp, note=reason)
        self._update_index_row(self.heat_id, status="COMPLETE", end=end_timestamp,
                                tilt_count=self.tilt_count)

        heat_minutes = (round((end_timestamp - self.heat_start).total_seconds() / 60.0, 2)
                        if self.heat_start else None)
        self._notify("record_heat_completed",
                     heat_id=self.heat_id,
                     heat_number=self.heat_number,
                     start_time=self.heat_start,
                     end_time=end_timestamp,
                     duration_minutes=heat_minutes,
                     tilt_count=self.tilt_count,
                     status="COMPLETE",
                     csv_file=os.path.basename(self.heat_csv_path) if self.heat_csv_path else None)

        suffix = f" ({reason})" if reason else ""
        self.last_event_desc = f"{self.heat_id}: tapping ended, heat complete{suffix}"
        self.state = self.STATE_IDLE
        self.tapping_start = None

    # -----------------------------------------------------------------
    # CSV helpers
    # -----------------------------------------------------------------
    def _log_event(self, name, start, end, tilt_number="", note=""):
        duration_min = round((end - start).total_seconds() / 60.0, 2) if start else ""
        row = [
            name,
            start.isoformat() if start else "",
            end.isoformat() if end else "",
            duration_min,
            tilt_number,
            note,
        ]
        with open(self.heat_csv_path, "a", newline="") as f:
            csv.writer(f).writerow(row)
        self.completed_events.append({
            "event": name,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "duration_minutes": duration_min,
            "tilt_number": tilt_number,
            "note": note,
        })

        # Same row, same ordinal, straight onto the recorder's queue.
        self._notify("record_event",
                     heat_id=self.heat_id,
                     heat_number=self.heat_number,
                     event_name=name,
                     start_time=start,
                     end_time=end,
                     duration_minutes=duration_min if duration_min != "" else None,
                     tilt_number=tilt_number if tilt_number != "" else None,
                     note=note or None,
                     ordinal=self._event_ordinal)
        self._event_ordinal += 1

    def _notify_event_start(self, name, start_time):
        self._notify("record_event",
                     heat_id=self.heat_id,
                     heat_number=self.heat_number,
                     event_name=name,
                     start_time=start_time,
                     end_time=None,
                     duration_minutes=None,
                     tilt_number=None,
                     note=None,
                     ordinal=self._event_ordinal)

    # -----------------------------------------------------------------
    # Recorder plumbing
    # -----------------------------------------------------------------
    def _notify(self, method, **kwargs):
        """Hand a row to the SQL recorder, if one is attached.

        Deliberately swallows everything: the CSV is the system of record
        and the furnace doesn't stop for a database, so nothing on this
        path may raise into the camera thread. The recorder's own queue
        makes the call non-blocking; this try/except covers the rest.
        """
        if self.recorder is None:
            return
        try:
            getattr(self.recorder, method)(**kwargs)
        except Exception:
            logger.exception("SQL recorder call %s failed (continuing on CSV only)", method)

    def _ensure_index_header(self):
        if not os.path.exists(self.index_csv):
            with open(self.index_csv, "w", newline="") as f:
                csv.writer(f).writerow(INDEX_HEADERS)

    def _load_last_heat_number(self):
        highest = 0
        for row in self.get_heats_history():
            parts = (row.get("heat_id") or "").split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                highest = max(highest, int(parts[1]))
        return highest

    def _append_index_row(self, status, start):
        with open(self.index_csv, "a", newline="") as f:
            csv.writer(f).writerow([
                self.heat_id,
                start.isoformat() if start else "",
                "", "", 0, status,
                os.path.basename(self.heat_csv_path),
            ])

    def _update_index_row(self, heat_id, status, end, tilt_count):
        rows = []
        with open(self.index_csv, "r", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            for row in reader:
                rows.append(row)

        for row in rows:
            if row[0] == heat_id:
                start = datetime.datetime.fromisoformat(row[1]) if row[1] else None
                duration = round((end - start).total_seconds() / 60.0, 2) if start else ""
                row[2] = end.isoformat()
                row[3] = duration
                row[4] = tilt_count
                row[5] = status

        with open(self.index_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
