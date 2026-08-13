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
  12 furnace_position_tilding  \
  18 furnace_position_up        }-> furnace leaves "normal" position.
                                    We don't yet know if this is the start
                                    of tapping or just an inspection tilt,
                                    so the app starts "watching".
  16 furnace_bottom_visiblecompletely
                                 -> if seen while the furnace is being
                                    watched, this CONFIRMS tapping. Sponge
                                    charging is closed out at the moment
                                    the furnace first left "normal"
                                    position, and tapping starts from that
                                    same moment.
  2  furnace_position_normal     -> furnace is back to normal position
                                    while being watched and 16 was never
                                    seen -> this was just a tilt (not
                                    tapping). tilt_count += 1 *unless* a
                                    tilt was already counted within the
                                    last 2 minutes (see "Tilt cooldown"
                                    below), and the app keeps waiting
                                    inside sponge charging.
  13 furnace_bottom_visiblepartially
                                 -> tracked for information only while
                                    watching; does not change any
                                    transition on its own under 130 min.

  From 130 minutes of sponge charging onward (fast-track):
  12 / 13 / 16 / 18 (any one of them)
                                 -> at this point the plant is confident
                                    any of these movements really is the
                                    start of tapping, so it's taken as
                                    tapping starting immediately - no need
                                    to also see the furnace return to
                                    normal first. Because the visual
                                    transition takes a little while to
                                    register, the boundary between sponge
                                    charging and tapping is backdated by
                                    2 minutes 13 seconds (added to
                                    tapping, deducted from sponge
                                    charging) from the moment of
                                    detection.

  Tilt cooldown: once a tilt has been *counted*, any further "furnace
  left normal, came back without tapping" cycle that resolves within 2
  minutes of that counted tilt is treated as a continuation of the same
  movement (camera/model flicker) rather than a brand-new tilt, so
  tilt_count doesn't climb rapidly. These are still written to the heat's
  CSV (as "tilt_ignored_cooldown") for audit purposes, just not counted.

  Starting mid-process (app restarted, or first launched, while a heat is
  already underway): if the first thing seen while IDLE is anything other
  than 3 (magnet_feeding_scrap), a heat is assumed to already be in
  progress and is picked up immediately from whichever stage that implies,
  rather than sitting in IDLE waiting indefinitely for the next scrap
  charging. See MID_PROCESS_SPONGE_CLASS_IDS / MID_PROCESS_TAPPING_CLASS_IDS
  below. The skipped earlier stage(s) are logged with an unknown start/
  duration instead of a guessed one.

  Ending a heat (tapping -> IDLE), whichever happens first:
    - 15 minutes have passed since tapping started, or
    - 3 magnet_feeding_scrap is detected (this also means the *next* heat's
      scrap charging has begun, so a new heat starts immediately from this
      same moment).

Assumptions made explicit (see README for more):
  - Tilt/tap watching only kicks in while sponge charging is the active
    stage, matching the described scrap -> sponge -> tapping flow.
  - "2:13" is read as 2 minutes 13 seconds (mm:ss), matching how the rest
    of the app displays durations.
  - Once tapping is confirmed (either way), the furnace-position classes
    (2/12/13/16/18) no longer drive any transition - only the 15-minute
    timer and class 3 end a heat, per the plant's own description.

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
CLS_FURNACE_BOTTOM_PARTIAL = 13
CLS_FURNACE_BOTTOM_FULL = 16
CLS_FURNACE_UP = 18

# Classes the state machine actually reacts to. Passed to the debouncer so
# it only tracks streaks for classes that matter.
RELEVANT_CLASS_IDS = {
    CLS_FURNACE_NORMAL,
    CLS_MAGNET_FEEDING_SCRAP,
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_FURNACE_TILTING,
    CLS_FURNACE_BOTTOM_PARTIAL,
    CLS_FURNACE_BOTTOM_FULL,
    CLS_FURNACE_UP,
}

# Once sponge charging has run for at least this long, any one of these
# classes is taken as tapping having started (fast-track - no need to also
# watch for the furnace returning to normal first).
FAST_TRACK_TAPPING_CLASS_IDS = {
    CLS_FURNACE_TILTING,
    CLS_FURNACE_BOTTOM_PARTIAL,
    CLS_FURNACE_BOTTOM_FULL,
    CLS_FURNACE_UP,
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
# 8 spange_feeder_onfurnace, or the furnace visibly moving/tilted/up
# -> already in sponge charging (or a tilt inside it); jump straight there.
MID_PROCESS_SPONGE_CLASS_IDS = {
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_FURNACE_TILTING,
    CLS_FURNACE_UP,
    CLS_FURNACE_BOTTOM_PARTIAL,
}
# 16 furnace_bottom_visiblecompletely on its own is the clearest possible
# signal that tapping is already happening -> jump straight to tapping.
MID_PROCESS_TAPPING_CLASS_IDS = {
    CLS_FURNACE_BOTTOM_FULL,
}

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
TILT_COOLDOWN_MINUTES = 2
TILT_COOLDOWN = datetime.timedelta(minutes=TILT_COOLDOWN_MINUTES)

SPONGE_FAST_TRACK_MINUTES = 130
SPONGE_FAST_TRACK_THRESHOLD = datetime.timedelta(minutes=SPONGE_FAST_TRACK_MINUTES)

# "2:13" -> 2 minutes 13 seconds, added to tapping / deducted from sponge
# charging when the fast-track rule fires.
TAPPING_BACKDATE = datetime.timedelta(minutes=2, seconds=13)

TAPPING_MAX_MINUTES = 15
TAPPING_MAX_DURATION = datetime.timedelta(minutes=TAPPING_MAX_MINUTES)


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
        self.last_tilt_time = None
        self.furnace_watch_active = False
        self.furnace_watch_start = None
        self.furnace_watch_saw_full_bottom = False
        self.furnace_watch_saw_partial_bottom = False

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

            if self.state == self.STATE_SPONGE_CHARGING:
                fast_track_hit = bool(newly_confirmed_ids & FAST_TRACK_TAPPING_CLASS_IDS)
                sponge_elapsed = timestamp - self.sponge_start if self.sponge_start else datetime.timedelta(0)

                if fast_track_hit and sponge_elapsed >= SPONGE_FAST_TRACK_THRESHOLD:
                    self._enter_tapping_fast_track(timestamp)
                else:
                    if (CLS_FURNACE_TILTING in newly_confirmed_ids or CLS_FURNACE_UP in newly_confirmed_ids) \
                            and not self.furnace_watch_active:
                        self._begin_furnace_watch(timestamp)

                    if CLS_FURNACE_BOTTOM_PARTIAL in newly_confirmed_ids and self.furnace_watch_active:
                        self.furnace_watch_saw_partial_bottom = True

                    if CLS_FURNACE_BOTTOM_FULL in newly_confirmed_ids and self.furnace_watch_active \
                            and not self.furnace_watch_saw_full_bottom:
                        self._confirm_tapping(timestamp)

                    if CLS_FURNACE_NORMAL in newly_confirmed_ids and self.furnace_watch_active:
                        self._resolve_furnace_watch(timestamp)

            self.last_updated = timestamp

    def check_timeouts(self, timestamp=None):
        """Evaluate any purely time-based transition (currently just the
        15-minute tapping timer). Safe to call frequently and with no new
        detections at all - call this periodically (e.g. once per camera
        frame, and once per status request) so a heat still closes out on
        time even if the classes above never fire again."""
        timestamp = timestamp or datetime.datetime.now()
        with self._lock:
            self._check_timeouts_locked(timestamp)

    def get_status(self):
        with self._lock:
            now = datetime.datetime.now()
            self._check_timeouts_locked(now)

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
                "furnace_watch_saw_partial_bottom": self.furnace_watch_saw_partial_bottom,
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
        """Must be called with self._lock already held."""
        if self.state == self.STATE_TAPPING and self.tapping_start is not None:
            if (timestamp - self.tapping_start) >= TAPPING_MAX_DURATION:
                # Cap the logged end time at exactly the 15-minute mark
                # rather than whenever this happened to be checked, so the
                # duration is deterministic regardless of check frequency.
                self._finalize_tapping(
                    self.tapping_start + TAPPING_MAX_DURATION,
                    reason=f"{TAPPING_MAX_MINUTES}-minute tapping timer elapsed",
                )

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
        self.last_tilt_time = None
        self.furnace_watch_active = False
        self.furnace_watch_saw_full_bottom = False
        self.furnace_watch_saw_partial_bottom = False
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

    def _end_scrap_start_sponge(self, timestamp):
        self._log_event("scrap_charging", self.scrap_start, timestamp)
        self.sponge_start = timestamp
        self.state = self.STATE_SPONGE_CHARGING
        self.last_event_desc = f"{self.heat_id}: sponge charging started"

    def _begin_furnace_watch(self, timestamp):
        self.furnace_watch_active = True
        self.furnace_watch_start = timestamp
        self.furnace_watch_saw_full_bottom = False
        self.furnace_watch_saw_partial_bottom = False
        self.last_event_desc = f"{self.heat_id}: furnace left normal position, watching..."

    def _confirm_tapping(self, timestamp):
        """Under-130-minute path: class 16 seen while watching a tilt."""
        self.furnace_watch_saw_full_bottom = True
        self._enter_tapping(self.furnace_watch_start, self.furnace_watch_start)

    def _enter_tapping_fast_track(self, timestamp):
        """130-minutes-and-over path: any of 12/13/16/18, backdated."""
        boundary = timestamp - TAPPING_BACKDATE
        if self.sponge_start and boundary < self.sponge_start:
            boundary = self.sponge_start
        self._enter_tapping(
            boundary, boundary,
            note=f"{SPONGE_FAST_TRACK_MINUTES}+ min sponge charging, backdated {TAPPING_BACKDATE}",
        )

    def _enter_tapping(self, tapping_start_time, sponge_end_time, note=""):
        """Shared by both ways of confirming tapping. Closes out sponge
        charging, starts the tapping clock, and clears any pending watch -
        from here on only the 15-minute timer / class 3 end the heat."""
        self._log_event("sponge_charging", self.sponge_start, sponge_end_time, note=note)
        self.tapping_start = tapping_start_time
        self.state = self.STATE_TAPPING

        self.furnace_watch_active = False
        self.furnace_watch_start = None
        self.furnace_watch_saw_full_bottom = False
        self.furnace_watch_saw_partial_bottom = False

        suffix = f" ({note})" if note else ""
        self.last_event_desc = f"{self.heat_id}: tapping started{suffix}"

    def _resolve_furnace_watch(self, timestamp):
        """Class 2 (furnace back to normal) seen while watching. Since
        tapping being confirmed always clears furnace_watch_active first,
        reaching this point means tapping was NOT confirmed - it's a tilt
        candidate, subject to the cooldown."""
        watch_start = self.furnace_watch_start
        self.furnace_watch_active = False
        self.furnace_watch_start = None
        self.furnace_watch_saw_full_bottom = False
        self.furnace_watch_saw_partial_bottom = False

        if self.last_tilt_time is not None and (timestamp - self.last_tilt_time) < TILT_COOLDOWN:
            # Within the cooldown window of the last *counted* tilt -
            # treat this as a continuation of the same movement rather
            # than a brand-new one, so the count doesn't climb rapidly.
            self._log_event("tilt_ignored_cooldown", watch_start, timestamp)
            self.last_event_desc = (
                f"{self.heat_id}: furnace back to normal - within "
                f"{TILT_COOLDOWN_MINUTES}-min cooldown, not counted"
            )
            return

        self.tilt_count += 1
        self.last_tilt_time = timestamp
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
