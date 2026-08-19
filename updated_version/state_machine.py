"""
HeatStateMachine
----------------
Turns a stream of *confirmed* class-name detections into the furnace process
timeline described by the plant: scrap charging -> sponge charging ->
tapping, plus a tilt counter for furnace movements that turn out NOT to be
tapping.

Logic (matches the classes by their text names, not numeric IDs - see the
CLS_* constants below for the exact strings used. Matching on names instead
of numbers means this logic keeps working even if the model is retrained
and its classes get renumbered, as long as the class *names* stay the same):

  Scrap charging starts (new heat begins) when EITHER of:
    - magnet_feeding_scrap is detected, or
    - magnet_moving_withscrap is detected positioned just above
      furnace_position_normal's box in the same frame (a derived signal -
      see CLS_MAGNET_ABOVE_FURNACE / camera_worker.py /
      detector.magnet_above_furnace_normal() and
      config.MAGNET_ABOVE_FURNACE_* for the spatial thresholds).
  If either fires while the *previous* heat is still in TAPPING, that heat
  is closed out immediately (see "Ending a heat" below) and the new heat
  starts from this same moment.
  Scrap charging cannot run longer than SCRAP_CHARGING_MAX_MINUTES (40 min
  by default) - see "Hard time caps" below.

  spange_feeder_onfurnace OR spange_feeder_engaged -> scrap charging ends,
  sponge charging starts. Sponge charging cannot run longer than
  SPONGE_CHARGING_MAX_MINUTES (140 min by default) - see "Hard time caps"
  below.

  Tapping starts EITHER of two ways (no other signal starts tapping):
    - furnace_position_tilding is held open for more than
      TILT_TO_TAPPING_MINUTES (3.5 min) without furnace_position_normal
      confirming it was just a tilt -> promoted straight into tapping,
      backdated to when the tilt itself began (see "Tilt duration ->
      tapping promotion" below), or
    - furnace_position_tilding is detected once sponge charging has
      already run for SPONGE_FAST_TRACK_MINUTES (130 min) or more -> taken
      as tapping starting immediately, no need to also wait out
      TILT_TO_TAPPING_MINUTES first (the "fast track").

  Watching for tapping while sponge charging is in progress (under the
  130-minute fast-track threshold):
  furnace_position_tilding    -> furnace leaves "normal" position. We
                                  don't yet know if this is the start of
                                  tapping or just an inspection tilt, so
                                  the app starts "watching".
  furnace_position_normal     -> furnace is back to normal position while
                                  being watched -> this was just a tilt
                                  (not tapping). tilt_count += 1, and the
                                  app keeps waiting inside sponge charging.

  furnace_position_normal     -> (while TAPPING) tapping has ended and the
                                  heat is complete.

  Starting mid-process (app restarted, or first launched, while a heat is
  already underway): if the first thing seen while IDLE is anything other
  than magnet_feeding_scrap / magnet_moving_withscrap-above-furnace, a
  heat is assumed to already be in progress and is picked up immediately
  from whichever stage that implies, rather than sitting in IDLE waiting
  indefinitely for the next scrap charging. See
  MID_PROCESS_SPONGE_CLASS_NAMES below - there's no class that on its own
  definitively means "tapping already in progress", so a mid-process
  pickup always starts from SPONGE_CHARGING and lets the normal tilting
  rules above sort out whether tapping has actually started.
  The skipped earlier stage(s) are logged with an unknown start/duration
  instead of a guessed one.

  Ending a heat (tapping -> IDLE), whichever happens first:
    - furnace_position_normal is detected while TAPPING, or
    - TAPPING_MAX_MINUTES (7 min) have passed since tapping started with
      no furnace_position_normal detected - see "Hard time caps" below, or
    - magnet_feeding_scrap (or the magnet-above-furnace signal) is
      detected (this also means the *next* heat's scrap charging has
      begun, so a new heat starts immediately from this same moment).

  Tilt duration -> tapping promotion (on top of the rules above):
  While the app is "watching" a tilt (see furnace_position_tilding above,
  under-130-minute case), if the furnace stays out of the normal position
  for more than TILT_TO_TAPPING_MINUTES (3.5 min) *without*
  furnace_position_normal ever confirming it was just a tilt, the watch is
  promoted straight into tapping - same as the 130-minute fast track, just
  triggered by how long this one tilt has run instead of by total
  sponge-charging time. This is a pure time-based check (see check_timeouts
  / _check_timeouts_locked below), so it fires even if no further
  detection ever arrives for that furnace movement.

  Hard time caps (independent of every rule above):
    - If scrap charging has been running for SCRAP_CHARGING_MAX_MINUTES
      (40 min) with no sponge_charging_start signal yet, the state machine
      forces the transition into sponge charging anyway, logged with a
      note, backdated to the cap being hit.
    - If sponge charging has been running for SPONGE_CHARGING_MAX_MINUTES
      (140 min) with tapping not yet underway, the state machine forces
      the transition into tapping anyway, logged with a note. In practice
      this is a backstop behind the 130-minute fast track above - it only
      fires if no furnace_position_tilding sighting arrived between minute
      130 and minute 140 to trigger the fast track itself.
    - If tapping has been running for TAPPING_MAX_MINUTES (7 min) with no
      furnace_position_normal detected yet, the state machine forces
      tapping to end (and the heat to complete) anyway, logged with a note.
  All three are pure time-based checks (see check_timeouts /
  _check_timeouts_locked below), so they fire even with no detections at
  all during that stretch.

  Active-fire watchdog (independent of the above):
  furnace_operation_activefire  -> heartbeat only; doesn't drive any stage
                                    transition by itself, but each
                                    confirmed sighting resets a clock for
                                    whatever heat is currently in progress
                                    (see note_activefire_seen).
  If a heat is in progress (any of SCRAP_CHARGING / SPONGE_CHARGING /
  TAPPING) and furnace_operation_activefire hasn't been confirmed for
  ACTIVEFIRE_TIMEOUT_MINUTES - counting from the heat's own start if it's
  never been seen at all yet - the heat is treated as invalid (camera lost
  the fire / furnace isn't actually running) and every trace of it is
  deleted: the SQL Heats + HeatEvents rows, the per-heat CSV file, and its
  row in the index CSV. The app then drops straight back to IDLE, ready
  for the next heat.
  NOTE: this watches the *whole* heat, including scrap charging. If your
  furnace can legitimately run some stage for longer than
  ACTIVEFIRE_TIMEOUT_MINUTES with no furnace_operation_activefire sighting
  on camera, that stage's heats will get wiped - tell me and this can be
  scoped to only start once sponge charging begins.

Assumptions made explicit (see README for more):
  - Tilt/tap watching only kicks in while sponge charging is the active
    stage, matching the described scrap -> sponge -> tapping flow.
  - Once tapping is confirmed (either way), only furnace_position_normal
    (to end it), the 15-minute timer, and magnet_feeding_scrap drive any
    further transition.

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

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classes used by the logic, matched by NAME (not numeric ID) - see the
# module docstring above for why. Every name comes from config.PROCESS_CLASSES
# - nothing here is hardcoded - so pointing any part of this logic at a
# different class (new model, renamed class, or just a different desired
# signal) is a one-line change in config.py, never in this file.
# ---------------------------------------------------------------------------
CLS_FURNACE_ACTIVEFIRE = config.PROCESS_CLASSES["furnace_activefire"]
CLS_FURNACE_NORMAL = config.PROCESS_CLASSES["furnace_normal"]
CLS_MAGNET_FEEDING_SCRAP = config.PROCESS_CLASSES["scrap_charging_start"]
CLS_FURNACE_TILTING = config.PROCESS_CLASSES["furnace_tilting"]
CLS_FURNACE_TILTING_ALT = config.PROCESS_CLASSES.get("furnace_tilting_alt")
CLS_SPONGE_FEEDER_ON_FURNACE = config.PROCESS_CLASSES["sponge_charging_start"]
CLS_SPONGE_FEEDER_ENGAGED = config.PROCESS_CLASSES["sponge_charging_start_alt"]

# Derived signal (not a raw model class - see camera_worker.py /
# detector.magnet_above_furnace_normal()) and the raw class name it's
# derived from, so camera_worker.py can import both from one place.
CLS_MAGNET_ABOVE_FURNACE = config.PROCESS_CLASSES["scrap_charging_magnet_alt"]
CLS_MAGNET_MOVING_WITHSCRAP = "magnet_moving_withscrap"

# Classes the state machine actually reacts to. Passed to the debouncer so
# it only tracks streaks for classes that matter.
RELEVANT_CLASS_NAMES = {
    CLS_FURNACE_NORMAL,
    CLS_MAGNET_FEEDING_SCRAP,
    CLS_MAGNET_ABOVE_FURNACE,
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_SPONGE_FEEDER_ENGAGED,
    CLS_FURNACE_ACTIVEFIRE,
    CLS_FURNACE_TILTING,
}
if CLS_FURNACE_TILTING_ALT:
    RELEVANT_CLASS_NAMES.add(CLS_FURNACE_TILTING_ALT)

# ---------------------------------------------------------------------------
# Mid-process start-up
# ---------------------------------------------------------------------------
# If the app is (re)started while a heat is already underway, we'd otherwise
# sit in IDLE forever waiting for the next magnet_feeding_scrap,
# missing the rest of the heat entirely. Instead, if the very first thing we
# see while IDLE is one of the classes below, assume a heat is already in
# progress and start tracking it immediately from whichever stage that class
# implies - scrap charging's own start time is simply unknown/unlogged in
# that case.
#
# spange_feeder_onfurnace / spange_feeder_engaged, or the furnace visibly
# moving -> already in sponge charging (or a tilt/tapping inside it); jump
# straight there and let the normal SPONGE_CHARGING logic below sort out
# whether this particular tilting sighting is just a tilt or is tapping.
MID_PROCESS_SPONGE_CLASS_NAMES = {
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_SPONGE_FEEDER_ENGAGED,
    CLS_FURNACE_TILTING,
}
if CLS_FURNACE_TILTING_ALT:
    MID_PROCESS_SPONGE_CLASS_NAMES.add(CLS_FURNACE_TILTING_ALT)
# No class definitively indicates tapping has already started on its own -
# tapping is only ever entered via the two rules in the module docstring
# above, both of which start from SPONGE_CHARGING.
MID_PROCESS_TAPPING_CLASS_NAMES = set()

CSV_HEADERS = ["event", "start_time", "end_time", "duration_minutes", "tilt_number", "note"]
INDEX_HEADERS = ["heat_id", "start_time", "end_time", "duration_minutes",
                  "tilt_count", "status", "csv_file"]

# Roughly matches the plant's own reference numbers (used only for display
# hints on the dashboard, never to force a transition).
EXPECTED_DURATION_MIN = {
    "scrap_charging": (20, 30),
    "sponge_charging": (60, 150),
    "tapping": (2, 7),
}

# ---------------------------------------------------------------------------
# Tunable timing rules - every value comes from config.py (see module
# docstring for what each one is for; see config.py for the actual numbers).
# ---------------------------------------------------------------------------
# Hard caps: a stage forced to end even with no qualifying detection yet.
SCRAP_CHARGING_MAX_MINUTES = config.SCRAP_CHARGING_MAX_MINUTES
SCRAP_CHARGING_MAX_THRESHOLD = datetime.timedelta(minutes=SCRAP_CHARGING_MAX_MINUTES)

SPONGE_CHARGING_MAX_MINUTES = config.SPONGE_CHARGING_MAX_MINUTES
SPONGE_CHARGING_MAX_THRESHOLD = datetime.timedelta(minutes=SPONGE_CHARGING_MAX_MINUTES)

SPONGE_FAST_TRACK_MINUTES = config.SPONGE_FAST_TRACK_MINUTES
SPONGE_FAST_TRACK_THRESHOLD = datetime.timedelta(minutes=SPONGE_FAST_TRACK_MINUTES)

# A tilt that's still open (furnace hasn't returned to "normal") after this
# long is no longer treated as a quick inspection tilt - it's promoted
# straight into tapping, whether or not furnace_position_normal ever confirms it.
TILT_TO_TAPPING_MINUTES = config.TILT_TO_TAPPING_MINUTES
TILT_TO_TAPPING_THRESHOLD = datetime.timedelta(minutes=TILT_TO_TAPPING_MINUTES)

# Tapping cannot run longer than this, full stop: if furnace_position_normal
# hasn't been detected by the time this many minutes have elapsed since
# tapping started, the state machine forces tapping to end (and the heat to
# complete) anyway.
TAPPING_MAX_MINUTES = config.TAPPING_MAX_MINUTES
TAPPING_MAX_THRESHOLD = datetime.timedelta(minutes=TAPPING_MAX_MINUTES)

# If a heat is in progress and furnace_operation_activefire hasn't
# been confirmed for this long, the heat is deleted (SQL + CSV) and the app
# returns to IDLE. See the module docstring for the important caveat about
# this covering the whole heat, including scrap charging.
ACTIVEFIRE_TIMEOUT_MINUTES = config.ACTIVEFIRE_TIMEOUT_MINUTES
ACTIVEFIRE_TIMEOUT = datetime.timedelta(minutes=ACTIVEFIRE_TIMEOUT_MINUTES)


class HeatStateMachine:
    STATE_IDLE = "IDLE"
    STATE_SCRAP_CHARGING = "SCRAP_CHARGING"
    STATE_SPONGE_CHARGING = "SPONGE_CHARGING"
    STATE_TAPPING = "TAPPING"

    RELEVANT_CLASS_NAMES = RELEVANT_CLASS_NAMES
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

        # Last time furnace_operation_activefire was confirmed,
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
            # timeout check below runs, so a call that both confirms furnace_operation_activefire
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
            scrap_charging_signal = CLS_MAGNET_FEEDING_SCRAP in newly_confirmed_ids \
                or CLS_MAGNET_ABOVE_FURNACE in newly_confirmed_ids

            if self.state == self.STATE_IDLE and not scrap_charging_signal:
                if newly_confirmed_ids & MID_PROCESS_TAPPING_CLASS_NAMES:
                    self._start_new_heat(timestamp, mid_process_stage="tapping")
                elif newly_confirmed_ids & MID_PROCESS_SPONGE_CLASS_NAMES:
                    self._start_new_heat(timestamp, mid_process_stage="sponge_charging")

            if scrap_charging_signal:
                if self.state == self.STATE_IDLE:
                    self._start_new_heat(timestamp)
                elif self.state == self.STATE_TAPPING:
                    # The next heat's scrap charging has begun -> the
                    # current heat's tapping (and the heat itself) ends
                    # right now, and the new heat starts from this same
                    # moment.
                    self._finalize_tapping(timestamp, reason="scrap charging signal detected")
                    self._start_new_heat(timestamp)

            sponge_charging_signal = CLS_SPONGE_FEEDER_ON_FURNACE in newly_confirmed_ids \
                or CLS_SPONGE_FEEDER_ENGAGED in newly_confirmed_ids

            if sponge_charging_signal and self.state == self.STATE_SCRAP_CHARGING:
                self._end_scrap_start_sponge(timestamp)

            if self.state == self.STATE_TAPPING:
                if CLS_FURNACE_NORMAL in newly_confirmed_ids:
                    self._finalize_tapping(timestamp, reason="furnace returned to normal")

            if self.state == self.STATE_SPONGE_CHARGING:
                sponge_elapsed = timestamp - self.sponge_start if self.sponge_start else datetime.timedelta(0)

                is_tilting = CLS_FURNACE_TILTING in newly_confirmed_ids or (CLS_FURNACE_TILTING_ALT and CLS_FURNACE_TILTING_ALT in newly_confirmed_ids)

                if is_tilting:
                    if sponge_elapsed >= SPONGE_FAST_TRACK_THRESHOLD:
                        self._enter_tapping(timestamp, timestamp, note=f">{SPONGE_FAST_TRACK_MINUTES} min sponge charging")
                    elif not self.furnace_watch_active:
                        self._begin_furnace_watch(timestamp)

                if self.state == self.STATE_SPONGE_CHARGING and CLS_FURNACE_NORMAL in newly_confirmed_ids:
                    if self.furnace_watch_active:
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
        """Called whenever furnace_operation_activefire is
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
        # Hard cap: scrap charging cannot run longer than
        # SCRAP_CHARGING_MAX_MINUTES. If no sponge_charging_start signal
        # has arrived by then, force the transition anyway.
        if (self.state == self.STATE_SCRAP_CHARGING
                and self.scrap_start is not None
                and (timestamp - self.scrap_start) >= SCRAP_CHARGING_MAX_THRESHOLD):
            self._end_scrap_start_sponge(
                timestamp,
                note=f"scrap charging hit {SCRAP_CHARGING_MAX_MINUTES} min cap",
            )

        # Tilt held open too long: no furnace_position_normal has confirmed it was "just a
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

        # Hard cap: sponge charging cannot run longer than
        # SPONGE_CHARGING_MAX_MINUTES. In practice this is a backstop
        # behind the 130-minute fast track - it only fires if no
        # furnace_position_tilding sighting arrived between the fast-track
        # minute and this cap to trigger tapping itself.
        if (self.state == self.STATE_SPONGE_CHARGING
                and self.sponge_start is not None
                and (timestamp - self.sponge_start) >= SPONGE_CHARGING_MAX_THRESHOLD):
            self._enter_tapping(
                timestamp, timestamp,
                note=f"sponge charging hit {SPONGE_CHARGING_MAX_MINUTES} min cap",
            )

        # Hard cap: tapping cannot run longer than TAPPING_MAX_MINUTES. If
        # furnace_position_normal hasn't been detected by then, force
        # tapping to end (and the heat to complete) anyway.
        if (self.state == self.STATE_TAPPING
                and self.tapping_start is not None
                and (timestamp - self.tapping_start) >= TAPPING_MAX_THRESHOLD):
            self._finalize_tapping(
                timestamp,
                reason=f"tapping hit {TAPPING_MAX_MINUTES} min cap without furnace_position_normal",
            )

        # Active-fire watchdog: a heat is running but furnace_operation_activefire hasn't been
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
        self.furnace_watch_start = None
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

    def _end_scrap_start_sponge(self, timestamp, note=""):
        self._log_event("scrap_charging", self.scrap_start, timestamp, note=note)
        self.sponge_start = timestamp
        self.state = self.STATE_SPONGE_CHARGING
        suffix = f" ({note})" if note else ""
        self.last_event_desc = f"{self.heat_id}: sponge charging started{suffix}"
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
        """furnace_position_normal seen while watching a furnace_position_tilding
        sighting -> tilt completed."""
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
        """Ends tapping and completes the heat. Triggered either by a
        furnace_position_normal detection, the TAPPING_MAX_MINUTES (7 min)
        cap, or a magnet_feeding_scrap / magnet-above-furnace detection
        (the next heat's scrap charging beginning)."""
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
