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

  Tilt duration -> tapping promotion (on top of the two rules above):
  While the app is "watching" a tilt (see 12 above, under-130-minute case),
  if the furnace stays out of the normal position for more than
  TILT_TO_TAPPING_MINUTES (1.9 min) *without* class 2 ever confirming it
  was just a tilt, the watch is promoted straight into tapping - same as
  the 130-minute fast track, just triggered by how long this one tilt has
  run instead of by total sponge-charging time. This is a pure time-based
  check (see check_timeouts / _check_timeouts_locked below), so it fires
  even if no further detection ever arrives for that furnace movement.

  Active-fire watchdog (independent of the above):
  9  furnace_operation_activefire  -> heartbeat only; doesn't drive any
                                       stage transition by itself, but each
                                       confirmed sighting resets a clock
                                       for whatever heat is currently in
                                       progress (see note_activefire_seen).
  If a heat is in progress (any of SCRAP_CHARGING / SPONGE_CHARGING /
  TAPPING) and class 9 hasn't been confirmed for ACTIVEFIRE_TIMEOUT_MINUTES
  (10 min) - counting from the heat's own start if it's never been seen at
  all yet - the heat is treated as invalid (camera lost the fire / furnace
  isn't actually running) and every trace of it is deleted: the SQL Heats
  + HeatEvents rows, the per-heat CSV file, and its row in the index CSV.
  The app then drops straight back to IDLE, ready for the next heat.
  NOTE: this watches the *whole* heat, including scrap charging. If your
  furnace can legitimately run some stage for more than 10 minutes with no
  class-9 sighting on camera, that stage's heats will get wiped - tell me
  and this can be scoped to only start once sponge charging begins.

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
CLS_FURNACE_ACTIVEFIRE = 9
CLS_FURNACE_TILTING = 12

# Classes the state machine actually reacts to. Passed to the debouncer so
# it only tracks streaks for classes that matter.
RELEVANT_CLASS_IDS = {
    CLS_FURNACE_NORMAL,
    CLS_MAGNET_FEEDING_SCRAP,
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_FURNACE_ACTIVEFIRE,
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

# A tilt that's still open (furnace hasn't returned to "normal") after this
# long is no longer treated as a quick inspection tilt - it's promoted
# straight into tapping, whether or not class 2 ever confirms it.
TILT_TO_TAPPING_MINUTES = 1.9
TILT_TO_TAPPING_THRESHOLD = datetime.timedelta(minutes=TILT_TO_TAPPING_MINUTES)

# If a heat is in progress and class 9 (furnace_operation_activefire) hasn't
# been confirmed for this long, the heat is deleted (SQL + CSV) and the app
# returns to IDLE. See the module docstring for the important caveat about
# this covering the whole heat, including scrap charging.
ACTIVEFIRE_TIMEOUT_MINUTES = 50
ACTIVEFIRE_TIMEOUT = datetime.timedelta(minutes=ACTIVEFIRE_TIMEOUT_MINUTES)


class HeatStateMachine:
    STATE_IDLE = "IDLE"
    STATE_SCRAP_CHARGING = "SCRAP_CHARGING"
    STATE_SPONGE_CHARGING = "SPONGE_CHARGING"
    STATE_TAPPING = "TAPPING"

    RELEVANT_CLASS_IDS = RELEVANT_CLASS_IDS
    EXPECTED_DURATION_MIN = EXPECTED_DURATION_MIN

    def __init__(self, data_dir="data/heats", index_csv="data/heats_index.csv",
                 recorder=None, furnace_id=None, heat_id_prefix="Heat"):
        self.data_dir = data_dir
        self.index_csv = index_csv
        # Optional db.SqlRecorder. It only ever puts a dict on a queue, so
        # calling it from here costs microseconds and can't block the
        # camera thread no matter what the database is doing. Leave it as
        # None and the class behaves exactly as it did before: CSV only.
        self.recorder = recorder

        # Tags every row this instance sends to the recorder (see _notify).
        # Left as None by default so old callers/tests see the exact same
        # behavior as before (db.py falls back to config.FURNACE_ID itself).
        self.furnace_id = furnace_id

        # Prefix used when minting heat IDs (e.g. "Heat" -> "Heat_003_...").
        # Two furnaces sharing one SQL Heats table must never produce the
        # same HeatId (it's the primary key), so each furnace instance is
        # given a distinct prefix. Must not contain "_" - heat_id is split
        # on "_" elsewhere to recover the numeric heat number.
        self.heat_id_prefix = heat_id_prefix
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

        # Last time class 9 (furnace_operation_activefire) was confirmed,
        # for whatever heat is currently in progress. Reset to the heat's
        # own start time each time a new heat begins - see the module
        # docstring for the active-fire watchdog this feeds.
        self.last_activefire_at = None

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
            # An activefire sighting updates the watchdog clock before the
            # timeout check below runs, so a call that both confirms class 9
            # *and* would otherwise be past the timeout doesn't get treated
            # as stale - if we can see fire right now, the heat is fine.
            if CLS_FURNACE_ACTIVEFIRE in newly_confirmed_ids:
                self.last_activefire_at = timestamp

            # Time-based transitions (the 15-minute tapping timer, the tilt
            # duration -> tapping promotion, the active-fire watchdog) get a
            # chance to fire first, using this call's timestamp as "now".
            # That keeps ordering correct if, say, a magnet_feeding_scrap
            # detection and a timer both would have fired by now.
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
        """Give purely time-based transitions a chance to fire even when no
        new detection has come in. Safe (and meant) to call on every frame,
        e.g. from the camera loop - process_detections() also runs the same
        check on every call it makes, so nothing is missed either way."""
        timestamp = timestamp or datetime.datetime.now()
        with self._lock:
            self._check_timeouts_locked(timestamp)

    def note_activefire_seen(self, timestamp=None):
        """Called whenever class 9 (furnace_operation_activefire) is
        currently confirmed - every processed frame it's visible on, not
        just the one frame it first became confirmed. A debounced class
        only shows up in process_detections() once per confirm/lose edge,
        which is fine for triggering a stage change but not enough to keep
        a multi-minute watchdog clock alive while the fire stays visible
        the whole time, so the camera worker calls this directly instead."""
        timestamp = timestamp or datetime.datetime.now()
        with self._lock:
            self.last_activefire_at = timestamp

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
                "last_activefire_at": self.last_activefire_at.isoformat() if self.last_activefire_at else None,
                "activefire_timeout_minutes": ACTIVEFIRE_TIMEOUT_MINUTES,
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
        """Caller must already hold self._lock."""
        # Tilt held open too long: no class 2 has confirmed it was "just a
        # tilt" within TILT_TO_TAPPING_MINUTES, so treat it as tapping
        # having started back when the tilt itself started.
        if (self.state == self.STATE_SPONGE_CHARGING
                and self.furnace_watch_active
                and self.furnace_watch_start is not None
                and (timestamp - self.furnace_watch_start) >= TILT_TO_TAPPING_THRESHOLD):
            self._enter_tapping(
                self.furnace_watch_start, self.furnace_watch_start,
                note=f"tilt held past {TILT_TO_TAPPING_MINUTES} min, treated as tapping",
            )

        # Active-fire watchdog: a heat is running but class 9 hasn't been
        # confirmed for ACTIVEFIRE_TIMEOUT_MINUTES (counting from the last
        # confirmed sighting, or from the heat's own start if it's never
        # been seen at all) -> the heat is invalid, wipe it.
        # Temporarily restrict this logic to furnace 4/2 only.
        if self.furnace_id == "4/2":
            if self.state != self.STATE_IDLE and self.heat_id is not None:
                baseline = self.last_activefire_at or self.heat_start
                if baseline is not None and (timestamp - baseline) >= ACTIVEFIRE_TIMEOUT:
                    self._abort_heat_no_activefire(timestamp)

    def _abort_heat_no_activefire(self, timestamp):
        """Class 9 hasn't been seen for ACTIVEFIRE_TIMEOUT while this heat
        was in progress. Delete every trace of it - the SQL Heats/HeatEvents
        rows, the per-heat CSV, and its row in the index CSV - so a later
        CSV->SQL backfill can't resurrect it, then drop back to IDLE."""
        heat_id = self.heat_id
        heat_csv_path = self.heat_csv_path
        since = self.last_activefire_at or self.heat_start
        minutes_since = (round((timestamp - since).total_seconds() / 60.0, 2)
                         if since else None)

        logger.warning(
            "%s: no furnace_operation_activefire confirmed for %s min while "
            "%s was in progress - deleting this heat's data (SQL + CSV) and "
            "returning to IDLE.",
            heat_id, minutes_since, self.state,
        )

        # SQL: fire-and-forget, same as every other recorder call - see
        # _notify(). Deletes both the Heats row and every HeatEvents row.
        self._notify("record_heat_deleted", heat_id=heat_id)

        # CSV: remove the per-heat file and its row in the index so a
        # future backfill (which just walks these files) has nothing left
        # to re-insert for this heat.
        try:
            if heat_csv_path and os.path.exists(heat_csv_path):
                os.remove(heat_csv_path)
        except OSError:
            logger.exception("Could not remove heat CSV %s", heat_csv_path)
        self._remove_index_row(heat_id)

        # Back to a clean slate - the next detection starts a brand new
        # heat. heat_number is deliberately left as-is (not decremented):
        # the deleted heat still leaves a gap rather than having its number
        # reused within the same run.
        self.heat_id = None
        self.heat_csv_path = None
        self.heat_start = None
        self._event_ordinal = 0
        self.scrap_start = None
        self.sponge_start = None
        self.tapping_start = None
        self.tilt_count = 0
        self.furnace_watch_active = False
        self.furnace_watch_start = None
        self.completed_events = []
        self.last_activefire_at = None
        self.state = self.STATE_IDLE
        self.last_event_desc = (
            f"{heat_id}: no active fire detected for {ACTIVEFIRE_TIMEOUT_MINUTES} min - "
            "heat data deleted"
        )

    def _start_new_heat(self, timestamp, mid_process_stage=None):
        """Starts a new heat. Normally this begins at scrap charging; if
        mid_process_stage is given ("sponge_charging" or "tapping"), the app
        was (re)started after that point already happened off-camera, so we
        skip straight to that stage instead, logging the earlier stage(s) as
        unknown-duration rather than guessing a start time for them."""
        self.heat_number += 1
        self.heat_id = f"{self.heat_id_prefix}_{self.heat_number:03d}_{timestamp.strftime('%Y%m%d_%H%M%S')}"
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
        # Fresh watchdog clock for this heat - see the active-fire watchdog
        # in the module docstring / _check_timeouts_locked.
        self.last_activefire_at = timestamp
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
        if self.furnace_id is not None:
            kwargs.setdefault("furnace_id", self.furnace_id)
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

    def _remove_index_row(self, heat_id):
        """Drop heat_id's row from the index CSV entirely (used when a heat
        is deleted, as opposed to _update_index_row which just edits it)."""
        if not os.path.exists(self.index_csv):
            return
        with open(self.index_csv, "r", newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = [row for row in reader if row and row[0] != heat_id]

        with open(self.index_csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
