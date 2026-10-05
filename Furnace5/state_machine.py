"""
HeatStateMachine
----------------
Turns a stream of *confirmed* class-ID detections into the furnace process
timeline described by the plant: scrap charging -> sponge charging ->
tapping, plus a tilt counter for furnace movements that turn out NOT to be
tapping.

Process sequence (43-class model + InfluxDB PYRO temperature):

  Stage 1: Scrap Charging (Heat Start)
  20 magnet_moving_with_scrap / 24 magnet_feeding_scrap / 21 furnace_operation_scrapfeeding / 37 human_feeding_scrap
    -> Scrap charging starts (new heat begins).
       If seen while previous heat is in TAPPING, that heat is closed out
       immediately and the new heat starts from this same moment.
    -> Maximum duration: 45 minutes. If no sponge class appears by 45 minutes,
       automatically transitions to sponge charging at scrap_start + 45 min.

  Stage 2: Sponge Iron Charging
  4 sponge_feeder_engaged / 15 sponge_feeder_onfurnace / 6 sponge_feeder_initialheat /
  19 sponge_feeder_stand / 27 magnet_moving_with_sponge / 2 magnet_feeding_sponge
    -> Scrap charging ends, sponge charging starts in the SAME heat.
    -> Reference duration: 90-120 minutes.

  Furnace Movement & Tapping vs. Tilt Decision:
  - Movement cycle: 34 (position up), 35 (bottom visible partially),
    17 (tilting), or 18 (bottom visible completely) starts a movement cycle
    (furnace_moving = True). Returning to 1 (normal) ends the movement cycle.
  - Tilt counting: Returning to normal counts as a tilt unless it occurs
    within 45 seconds of the previous counted tilt's end (TILT_MIN_GAP = 45s).

  - Tapping is entered by one of two paths:

      (A) Class 36 (laddle_placed_fortapping) confirmed -> tapping starts
          IMMEDIATELY at that timestamp. No pre-roll, no candidate window.
          The heat remains in TAPPING until a normal end-of-tapping rule
          fires (below).

      (B) Class 18 (furnace_bottom_visiblecompletely) opens a 5-minute
          tapping candidate window. Tapping is confirmed if:
            * class 25 (laddle_moving_withmeltiron) is detected.
          (PYRO data changes do NOT confirm tapping).
          Tapping start is backdated by a 90-second pre-roll before class 18
          (tap_candidate_start - 90s, clamped to sponge_start), and the
          heat completes immediately on confirmation.
          If the window expires with no signal, the movement resolves as
          a tilt.

  - End of tapping (identical for both paths):
      * class 1 (furnace_position_normal) detected   -> tapping ends,
      * 15 minutes pass since tapping_start          -> TAPPING_MAX_MINUTES cap,
      * next scrap class (24/21/37) detected         -> tapping ends and the
        next heat begins from the scrap timestamp.

  Heat Completion (Tapping -> IDLE):
    - 1 (furnace_position_normal) is detected while TAPPING, or
    - 15 minutes have passed since tapping started (safety timeout cap), or
    - 24/21/37 (scrap feeding) detected (next heat begins immediately).

  Active-Fire Watchdog:
  5 furnace_operation_activefire -> Heartbeat signal; confirmed sightings
       reset the active fire timeout clock. If no active fire is
       seen for the configured timeout (5 min) during an active heat, the heat is aborted/purged.

  Auxiliary Events:
  42 human_carring_silicomanganese / 41 human_feeding_silicomanganese
    -> Logged as silicomanganese alloy additions in HeatEvents.
  29 magnet_moving_slag
    -> Logged as slag removal / deslagging in HeatEvents.
  Maintenance Classes (0, 7, 28, 39, 31):
    0 coil_position_placednearfurnace
    7 furnace_operation_preheating
    28 furnace_operation_undermaintenance
    39 furma_position_placednearfurnace
    31 furnace_operation_furmareplaced
    -> Logged in furnace status / debug frame as furnace under maintenance.
"""

import csv
import datetime
import json
import logging
import os
import random
import threading
import time

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Class IDs used by the logic (mapped to the NEW 43-class YOLO model)
# ---------------------------------------------------------------------------
CLS_COIL_POSITION_PLACEDNEARFURNACE = 0
CLS_FURNACE_NORMAL = 1
CLS_MAGNET_FEEDING_SPONGE = 2
CLS_SPONGE_FEEDER_FARFROMFURNACE = 3
CLS_SPONGE_FEEDER_ENGAGED = 4
CLS_FURNACE_ACTIVEFIRE = 5
CLS_SPONGE_FEEDER_INITIALHEAT = 6
CLS_FURNACE_OPERATION_PREHEATING = 7
CLS_NEGATIVE_SCRAPONGROUND = 8
CLS_NEGATIVE_FAN = 9
CLS_NEGATIVE_RAMMINGMUSSCONTAINER = 10
CLS_NEGATIVE_PILLERS = 11
CLS_MAGNET_MOVING_IDLE = 12
CLS_NEGATIVE_SLAG = 13
CLS_NEGATIVE_CRANEBEAM = 14
CLS_SPONGE_FEEDER_ON_FURNACE = 15
CLS_NEGATIVE_OTHERFURNACES = 16
CLS_FURNACE_TILTING = 17
CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY = 18
CLS_SPONGE_FEEDER_STAND = 19
CLS_MAGNET_MOVING_WITH_SCRAP = 20
CLS_FURNACE_OPERATION_SCRAPFEEDING = 21
CLS_NEGATIVE_FURNACESMOKE = 22
CLS_NEGATIVE_SUNLIGHT = 23
CLS_MAGNET_FEEDING_SCRAP = 24
CLS_LADDLE_MOVING_WITHMELTIRON = 25
CLS_SPONGE_FEEDER_NOTENGAGED = 26
CLS_MAGNET_MOVING_WITH_SPONGE = 27
CLS_FURNACE_OPERATION_UNDERMAINTENANCE = 28
CLS_MAGNET_MOVING_SLAG = 29
CLS_NEGATIVE_MAGNETONGROUND = 30
CLS_FURNACE_OPERATION_FURMAREPLACED = 31
CLS_MAGNET_MOVING_WITHSHAKER = 32
CLS_LADDLE_MOVING_EMPTY = 33
CLS_FURNACE_POSITION_UP = 34
CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY = 35
CLS_LADDLE_PLACED_FORTAPPING = 36
CLS_HUMAN_FEEDING_SCRAP = 37
CLS_NEGATIVE_NOTVISIBLE = 38
CLS_FURMA_POSITION_PLACEDNEARFURNACE = 39
CLS_NEGATIVE_HANGGINGCREATE = 40
CLS_HUMAN_FEEDING_SILICOMANGANESE = 41
CLS_HUMAN_CARRYING_SILICOMANGANESE = 42

# Logical groupings
SCRAP_CHARGING_CLASS_IDS = {
    CLS_MAGNET_FEEDING_SCRAP,            # was 17, now 24
    CLS_FURNACE_OPERATION_SCRAPFEEDING,  # was 19, now 21
    CLS_HUMAN_FEEDING_SCRAP,             # new 37
}

SPONGE_CHARGING_CLASS_IDS = {
    CLS_SPONGE_FEEDER_ENGAGED,           # was 6, now 4
    CLS_SPONGE_FEEDER_ON_FURNACE,        # was 10, now 15
    CLS_MAGNET_FEEDING_SPONGE,           # was 37, now 2
}

# Any of these classes indicate furnace movement away from normal
# position during SPONGE CHARGING. Temperature decides whether it is
# tapping or a tilt.
FURNACE_MOVEMENT_CLASS_IDS = {
    CLS_FURNACE_POSITION_UP,                # was 2, now 34
    CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY,   # was 8, now 35
    CLS_FURNACE_TILTING,                    # was 18, now 17
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,  # was 24, now 18
}

FURNACE_NORMAL_CLASS_IDS = {
    CLS_FURNACE_NORMAL,                     # was 9, now 1
}

ACTIVEFIRE_CLASS_IDS = {
    CLS_FURNACE_ACTIVEFIRE,                 # was 3, now 5
}

# Classes that open the 5-minute tapping candidate window.
# Only class 18 remains: class 36 (laddle_placed_fortapping) is now a
# first-class tapping trigger handled separately - see _handle_sponge_movement.
TAPPING_CANDIDATE_OPENERS = {
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,   # 18
}

# Maintenance classes indicating the furnace is under maintenance
UNDERMAINTENANCE_CLASS_IDS = {
    CLS_COIL_POSITION_PLACEDNEARFURNACE,     # 0
    CLS_FURNACE_OPERATION_PREHEATING,        # 7
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,  # 28
    CLS_FURMA_POSITION_PLACEDNEARFURNACE,    # 39
    CLS_FURNACE_OPERATION_FURMAREPLACED,     # 31
}

# Classes the state machine actually reacts to. Passed to the debouncer so
# it only tracks streaks for classes that matter.
RELEVANT_CLASS_IDS = (
    SCRAP_CHARGING_CLASS_IDS
    | SPONGE_CHARGING_CLASS_IDS
    | FURNACE_MOVEMENT_CLASS_IDS
    | FURNACE_NORMAL_CLASS_IDS
    | ACTIVEFIRE_CLASS_IDS
    | TAPPING_CANDIDATE_OPENERS
    | UNDERMAINTENANCE_CLASS_IDS
    | {
        CLS_LADDLE_PLACED_FORTAPPING,           # 36
        CLS_LADDLE_MOVING_WITHMELTIRON,         # 25
        CLS_LADDLE_MOVING_EMPTY,                # was 13, now 33
        CLS_HUMAN_CARRYING_SILICOMANGANESE,     # was 21, now 42
        CLS_HUMAN_FEEDING_SILICOMANGANESE,      # was 22, now 41
        CLS_MAGNET_MOVING_SLAG,                 # was 36, now 29
        CLS_HUMAN_FEEDING_SCRAP,                # new 37
        CLS_SPONGE_FEEDER_INITIALHEAT,          # new 6
        CLS_SPONGE_FEEDER_STAND,                # new 19
    }
)

# ---------------------------------------------------------------------------
# Mid-process start-up recovery
# ---------------------------------------------------------------------------
MID_PROCESS_SPONGE_CLASS_IDS = (
    SPONGE_CHARGING_CLASS_IDS
    | FURNACE_MOVEMENT_CLASS_IDS
)

CSV_HEADERS = ["event", "start_time", "end_time", "duration_minutes", "tilt_number", "note"]
INDEX_HEADERS = ["heat_id", "start_time", "end_time", "duration_minutes",
                  "tilt_count", "status", "csv_file", "last_updated"]

# Expected duration ranges (used for dashboard presentation and progress bars).
EXPECTED_DURATION_MIN = {
    "scrap_charging": (0, 45),
    "sponge_charging": (90, 120),
    "tapping": (10, 20),
}

# ---------------------------------------------------------------------------
# Tunable timing rules
# ---------------------------------------------------------------------------
# Maximum scrap charging duration before automatic transition to sponge
SCRAP_MAX_MINUTES = 45
SCRAP_MAX_THRESHOLD = datetime.timedelta(minutes=SCRAP_MAX_MINUTES)

# Maximum tapping duration before automatic heat completion if normal position is missed.
TAPPING_MAX_MINUTES = 15
TAPPING_MAX_THRESHOLD = datetime.timedelta(minutes=TAPPING_MAX_MINUTES)

# Active-fire watchdog timeout.
ACTIVEFIRE_TIMEOUT_MINUTES = config.ACTIVEFIRE_TIMEOUT_MINUTES
ACTIVEFIRE_TIMEOUT = datetime.timedelta(minutes=ACTIVEFIRE_TIMEOUT_MINUTES)

AWAY_FROM_NORMAL_CLASS_IDS = {
    CLS_FURNACE_POSITION_UP,                # 34
    CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY,   # 35
    CLS_FURNACE_TILTING,                    # 17
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,  # 18
}
TILT_MIN_GAP = datetime.timedelta(seconds=45)
TAPPING_CANDIDATE_WINDOW = datetime.timedelta(minutes=5)
TAPPING_PRE_ROLL = datetime.timedelta(seconds=90)

# How much to add to the recorded tapping end time before closing the heat.
# 1.25 min = midpoint of the 1-1.5 min window you want.
TAPPING_END_EXTENSION = datetime.timedelta(minutes=1.25)


class HeatStateMachine:
    STATE_IDLE = "IDLE"
    STATE_SCRAP_CHARGING = "SCRAP_CHARGING"
    STATE_SPONGE_CHARGING = "SPONGE_CHARGING"
    STATE_TAPPING = "TAPPING"

    RELEVANT_CLASS_IDS = RELEVANT_CLASS_IDS
    EXPECTED_DURATION_MIN = EXPECTED_DURATION_MIN

    def __init__(self, data_dir="data/heats", index_csv="data/heats_index.csv",
                 recorder=None, furnace_id=None, heat_id_prefix="Heat",
                 temperature_monitor=None, on_activefire_loss=None):
        self.data_dir = data_dir
        self.index_csv = index_csv
        self.recorder = recorder
        self.furnace_id = furnace_id
        self.heat_id_prefix = heat_id_prefix
        self.temperature_monitor = temperature_monitor
        self._on_activefire_loss = on_activefire_loss

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

        self.heat_start = None
        self._event_ordinal = 0

        self.scrap_start = None
        self.sponge_start = None
        self.tapping_start = None

        self.tilt_count = 0

        self.furnace_moving = False
        self.furnace_move_start = None
        self.last_tilt_end = None

        self.tap_candidate_active = False
        self.tap_candidate_start = None      # moment class 18 was confirmed
        self.tap_candidate_deadline = None
        self.tap_candidate_opener = None     # "furnace_bottom_visiblecompletely (18)" etc.
        self._tap_monitors = []
        self._tap_baselines = {}             # id(monitor) -> (value, seq)

        self.pending_tilt = None             # (move_start, normal_time) held while a tapping check runs

        # Tapping started via ladle (class 36) is only over once the furnace
        # has actually moved (17 / 18 / 34 / 35) and then come back to normal
        # (1). This prevents a single stray "normal" frame from ending tapping
        # the instant the ladle is placed.
        self.tapping_requires_movement = False
        self.tapping_movement_seen = False

        self.last_activefire_at = datetime.datetime.now()
        self._camera_connected = True
        self._camera_down_since = None

        self.completed_events = []
        self.last_event_desc = "Waiting for scrap charging to begin..."
        self.last_updated = None

        self._startup_recovery_available = True
        self._scrap_charging_confirmed = False

        # Heartbeat throttle for the index CSV's last_updated column.
        # _touch_index_row() is called on every frame but only rewrites the file
        # at most once every _index_touch_interval seconds.
        self._last_index_touch_mono = 0.0
        self._index_touch_interval = 15.0

        # Pending "capture this frame" requests for CameraWorker to drain on
        # its next frame. Populated by _request_frame() at every stage
        # boundary; drained atomically by get_pending_frame_requests().
        self._frame_requests = []

        # NEW: on startup, if the last heat is IN_PROGRESS and recent,
        # take over from it instead of starting a brand-new heat.
        self._resume_incomplete_heat_if_recent()

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    def process_detections(self, newly_confirmed_ids, timestamp=None):
        """Feed a set of newly-confirmed class IDs into the state machine."""
        timestamp = timestamp or datetime.datetime.now()
        newly_confirmed_ids = set(newly_confirmed_ids) if newly_confirmed_ids else set()

        with self._lock:
            # If camera was suspended pending connection, arriving detections mark it connected
            if not self._camera_connected:
                self._camera_connected = True
                self._camera_down_since = None
                self.last_activefire_at = timestamp

            # Active-fire sighting updates the watchdog clock
            if newly_confirmed_ids & ACTIVEFIRE_CLASS_IDS:
                self.last_activefire_at = timestamp

            # Time-based transitions (scrap 45m cap, tapping 15m cap, active-fire watchdog)
            self._check_timeouts_locked(timestamp)

            if not newly_confirmed_ids:
                self.last_updated = timestamp
                return

            # Mid-process start-up recovery
            if (self.state == self.STATE_IDLE
                    and self._startup_recovery_available
                    and not (newly_confirmed_ids & SCRAP_CHARGING_CLASS_IDS)):

                if newly_confirmed_ids & MID_PROCESS_SPONGE_CLASS_IDS:
                    self._start_new_heat(
                        timestamp,
                        mid_process_stage="sponge_charging"
                    )

            # Scrap charging start (or close tapping if it was still open)
            if newly_confirmed_ids & SCRAP_CHARGING_CLASS_IDS:
                if self.state == self.STATE_IDLE:
                    self._start_new_heat(timestamp)
                elif self.state == self.STATE_TAPPING:
                    self._finalize_tapping(timestamp, reason="scrap feeding detected")
                    self._start_new_heat(timestamp)
                elif self.state == self.STATE_SCRAP_CHARGING:
                    # Real scrap-feed class confirmed -> the provisional heat is
                    # now a genuine heat, so the 45-min scrap cap must NOT abort it.
                    self._scrap_charging_confirmed = True
                    self._save_active_state()
                # (no case for STATE_SPONGE_CHARGING — scrap during sponge
                # charging is now ignored, sponge keeps running)

            # Transition from scrap charging to sponge charging
            if (newly_confirmed_ids & SPONGE_CHARGING_CLASS_IDS) and self.state == self.STATE_SCRAP_CHARGING:
                self._end_scrap_start_sponge(timestamp)

            # Tapping completion (class 1 returns to normal).
            # For the class-36 path, class 1 only counts as the end of tapping
            # AFTER the furnace has actually moved away from normal (tilt / bottom
            # visible) and come back.
            if self.state == self.STATE_TAPPING:
                if newly_confirmed_ids & FURNACE_MOVEMENT_CLASS_IDS:
                    self.tapping_movement_seen = True
                if newly_confirmed_ids & FURNACE_NORMAL_CLASS_IDS:
                    if (not self.tapping_requires_movement) or self.tapping_movement_seen:
                        self._finalize_tapping(
                            timestamp,
                            reason="furnace returned to normal",
                            auto_next=True,
                        )
                    else:
                        self.last_event_desc = (
                            f"{self.heat_id}: tapping (ladle-placed) waiting for "
                            "furnace movement before completing"
                        )

            if self.state == self.STATE_SPONGE_CHARGING:
                self._handle_sponge_movement(newly_confirmed_ids, timestamp)

            # Maintenance detection (both during active heat or when idle)
            maint_detected = newly_confirmed_ids & UNDERMAINTENANCE_CLASS_IDS
            if maint_detected:
                cid = sorted(maint_detected)[0]
                cname = config.CLASS_NAMES.get(cid, str(cid))
                self.last_event_desc = f"{self.heat_id or 'Furnace'}: furnace under maintenance"
                self._request_frame("furnace_undermaintenance", timestamp, f"furnace operation undermaintenance ({cname} - {cid})")

            # Auxiliary process events during an active heat
            if self.state != self.STATE_IDLE:
                if newly_confirmed_ids & {CLS_HUMAN_FEEDING_SILICOMANGANESE, CLS_HUMAN_CARRYING_SILICOMANGANESE}:
                    self._log_event("silicomanganese_addition", timestamp, timestamp, note="silicomanganese addition confirmed")
                    self._request_frame("silicomanganese_addition", timestamp, "human feeding/carrying silicomanganese (41/42)")
                if CLS_MAGNET_MOVING_SLAG in newly_confirmed_ids:
                    self._log_event("slag_removal", timestamp, timestamp, note="magnet moving slag")
                    self._request_frame("slag_removal", timestamp, "magnet moving slag (29)")

            self.last_updated = timestamp

    def check_timeouts(self, timestamp=None):
        """Checks timeout rules and ongoing movement watch temperature."""
        timestamp = timestamp or datetime.datetime.now()
        with self._lock:
            self._check_timeouts_locked(timestamp)

    def note_activefire_seen(self, timestamp=None):
        """Updates active-fire watchdog clock when class 5 is observed."""
        timestamp = timestamp or datetime.datetime.now()
        with self._lock:
            self.last_activefire_at = timestamp

    def note_camera_state(self, connected):
        """Called by CameraWorker when the RTSP stream connects or drops.

        While the camera is down, timeouts that depend on seeing the process
        (the active-fire watchdog) are suspended, so a network outage cannot
        delete a live heat. On reconnect, the active-fire clock is reset so
        the watchdog doesn't fire immediately just because class 5 was never
        seen while the camera was offline.
        """
        with self._lock:
            if connected == self._camera_connected:
                return

            self._camera_connected = connected
            now = datetime.datetime.now()

            if not connected:
                self._camera_down_since = now
                logger.warning(
                    "%s: camera down - active-fire watchdog suspended", self.heat_id,
                )
            else:
                down_for = None
                if self._camera_down_since is not None:
                    down_for = round((now - self._camera_down_since).total_seconds(), 1)
                self._camera_down_since = None

                # Reset the watchdog clock so we don't fire on the first frame
                # back just because no active fire was seen during the outage.
                self.last_activefire_at = now

                logger.info(
                    "%s: camera back after %s s - watchdog resumed", self.heat_id, down_for,
                )

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
                "furnace_watch_active": self.furnace_moving or self.tap_candidate_active,
                "furnace_moving": self.furnace_moving,
                "furnace_move_start": self.furnace_move_start.isoformat() if self.furnace_move_start else None,
                "tapping_candidate_active": self.tap_candidate_active,
                "tapping_candidate_start": self.tap_candidate_start.isoformat() if self.tap_candidate_start else None,
                "tapping_candidate_deadline": self.tap_candidate_deadline.isoformat() if self.tap_candidate_deadline else None,
                "last_tilt_end": self.last_tilt_end.isoformat() if self.last_tilt_end else None,
                "tapping_pre_roll_seconds": TAPPING_PRE_ROLL.total_seconds(),
                "tapping_candidate_window_minutes": TAPPING_CANDIDATE_WINDOW.total_seconds() / 60.0,
                "scrap_max_minutes": SCRAP_MAX_MINUTES,
                "tapping_max_minutes": TAPPING_MAX_MINUTES,
                "pyro_baselines": {
                    getattr(m, "field", f"monitor_{i}"): (
                        self._tap_baselines.get(id(m))[0]
                        if self._tap_baselines.get(id(m)) else None
                    )
                    for i, m in enumerate(self._tap_monitors)
                },
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
        if s.get("tapping_candidate_active"):
            lines.append("Furnace: bottom visible (tapping check running)")
        elif s.get("furnace_moving"):
            lines.append("Furnace: Moving")
        return lines

    def get_heats_history(self):
        if not os.path.exists(self.index_csv):
            return []
        with open(self.index_csv, "r", newline="") as f:
            return list(csv.DictReader(f))

    def get_heat_csv_path(self, heat_id):
        path = os.path.join(self.data_dir, f"{heat_id}.csv")
        return path if os.path.exists(path) else None

    def get_pending_frame_requests(self):
        """Atomically drain and return the frame-capture queue. Called by
        CameraWorker every frame."""
        with self._lock:
            requests = list(self._frame_requests)
            self._frame_requests.clear()
            return requests

    def _request_frame(self, event_name, timestamp, description="", extra_meta=None):
        """Queue a frame capture tagged with the current stage. CameraWorker
        fulfils it on its next loop (saves raw frame + annotated frame + JSON sidecar)."""
        req = {
            "heat_id": self.heat_id,
            "furnace_id": self.furnace_id,
            "event_name": event_name,
            "timestamp": (timestamp or datetime.datetime.now()).isoformat(),
            "description": description,
            "stage": self.state,
            "tilt_count": getattr(self, "tilt_count", 0),
            "furnace_moving": getattr(self, "furnace_moving", False),
            "tapping_candidate_active": getattr(self, "tap_candidate_active", False),
            "last_event": getattr(self, "last_event_desc", ""),
        }
        if extra_meta and isinstance(extra_meta, dict):
            req.update(extra_meta)
        self._frame_requests.append(req)

    # -----------------------------------------------------------------
    # Internal transition handlers
    # -----------------------------------------------------------------
    def _check_timeouts_locked(self, timestamp):
        """Caller must already hold self._lock."""
        self._touch_index_row()

        # Active-fire watchdog: a heat is running but class 5 hasn't been
        # confirmed for ACTIVEFIRE_TIMEOUT_MINUTES -> purge the heat.
        # Skipped while the camera is down: no frames means we can't know
        # whether the fire is still going, so the clock is paused up to
        # CAMERA_OUTAGE_GRACE_MINUTES.
        if self.state != self.STATE_IDLE and self.heat_id is not None:
            grace_seconds = getattr(config, "CAMERA_OUTAGE_GRACE_MINUTES", 30) * 60
            skip_for_camera = (
                not self._camera_connected
                and (self._camera_down_since is None or (timestamp - self._camera_down_since).total_seconds() < grace_seconds)
            )
            if not skip_for_camera:
                baseline = self.last_activefire_at or self.heat_start
                if baseline is not None and (timestamp - baseline) >= ACTIVEFIRE_TIMEOUT:
                    self._abort_heat_no_activefire(timestamp)

        # 45-minute scrap charging maximum timeout.
        if (self.state == self.STATE_SCRAP_CHARGING
                and self.scrap_start is not None
                and (timestamp - self.scrap_start) >= SCRAP_MAX_THRESHOLD):

            if self._scrap_charging_confirmed:
                # A real scrap-feed class was seen - keep existing behaviour.
                sponge_start = self.scrap_start + SCRAP_MAX_THRESHOLD
                logger.info(
                    "%s: scrap charging reached 45 min cap without sponge detection. "
                    "Auto-transitioning to sponge charging at %s.",
                    self.heat_id, sponge_start
                )
                self._end_scrap_start_sponge(
                    sponge_start,
                    note="auto-transition after 45 min scrap maximum"
                )
            else:
                # Provisional heat from tap-end auto-next never saw scrap → abort.
                self._abort_scrap_not_confirmed(timestamp)

        if self.state == self.STATE_SPONGE_CHARGING and self.tap_candidate_active:
            self._check_tapping_candidate(timestamp)

        # 15-minute tapping timeout cap: safety completion if class 1 was missed
        if (self.state == self.STATE_TAPPING
                and self.tapping_start is not None
                and (timestamp - self.tapping_start) >= TAPPING_MAX_THRESHOLD):
            self._finalize_tapping(
                timestamp,
                reason=f"tapping hit {TAPPING_MAX_MINUTES} min cap without furnace_position_normal",
                auto_next=True,
            )

    def _abort_heat_no_activefire(self, timestamp):
        """Class 5 hasn't been seen for ACTIVEFIRE_TIMEOUT while this heat was in progress."""
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

        self._notify("record_heat_deleted", heat_id=heat_id)
        self._request_frame("heat_aborted", timestamp,
                            f"no active fire for {ACTIVEFIRE_TIMEOUT_MINUTES} min")

        try:
            if heat_csv_path and os.path.exists(heat_csv_path):
                os.remove(heat_csv_path)
        except OSError:
            logger.exception("Could not remove heat CSV %s", heat_csv_path)
        self._remove_index_row(heat_id)
        self._clear_active_state(heat_id)

        self.heat_id = None
        self.heat_csv_path = None
        self.heat_start = None
        self._event_ordinal = 0
        self.scrap_start = None
        self.sponge_start = None
        self.tapping_start = None
        self.tilt_count = 0
        self._scrap_charging_confirmed = False
        self._reset_furnace_watch()
        self.completed_events = []
        self.last_activefire_at = datetime.datetime.now()
        self.state = self.STATE_IDLE
        self.last_event_desc = (
            f"{heat_id}: no active fire detected for {ACTIVEFIRE_TIMEOUT_MINUTES} min - "
            "heat data deleted"
        )

        if self._on_activefire_loss is not None:
            try:
                self._on_activefire_loss()
            except Exception:
                logger.exception("activefire-loss callback failed")

    def _abort_scrap_not_confirmed(self, timestamp):
        """A heat was started (normally right after tapping ended) but no real
        scrap-charging class (24 / 21 / 37) was confirmed within
        SCRAP_MAX_MINUTES. Delete the whole heat's data and go back to IDLE.
        """
        heat_id = self.heat_id
        heat_csv_path = self.heat_csv_path

        logger.warning(
            "%s: no scrap-charging class confirmed within %d min of heat start - "
            "deleting this heat's data (SQL + CSV) and returning to IDLE.",
            heat_id, SCRAP_MAX_MINUTES,
        )

        self._notify("record_heat_deleted", heat_id=heat_id)
        self._request_frame(
            "heat_aborted_no_scrap", timestamp,
            f"no scrap charging within {SCRAP_MAX_MINUTES} min",
        )

        try:
            if heat_csv_path and os.path.exists(heat_csv_path):
                os.remove(heat_csv_path)
        except OSError:
            logger.exception("Could not remove heat CSV %s", heat_csv_path)
        self._remove_index_row(heat_id)
        self._clear_active_state(heat_id)

        # Reset all heat state - mirrors _abort_heat_no_activefire.
        self.heat_id = None
        self.heat_csv_path = None
        self.heat_start = None
        self._event_ordinal = 0
        self.scrap_start = None
        self.sponge_start = None
        self.tapping_start = None
        self.tilt_count = 0
        self._scrap_charging_confirmed = False
        self._reset_furnace_watch()
        self.completed_events = []
        self.last_activefire_at = datetime.datetime.now()
        self.state = self.STATE_IDLE
        self.last_event_desc = (
            f"{heat_id}: no scrap charging detected within "
            f"{SCRAP_MAX_MINUTES} min - heat data deleted"
        )

    def _start_new_heat(self, timestamp, mid_process_stage=None, scrap_confirmed=True):
        self._startup_recovery_available = False
        self.heat_number += 1
        self.heat_id = f"{self.heat_id_prefix}_{self.heat_number:03d}_{timestamp.strftime('%Y%m%d_%H%M%S')}"
        self.heat_csv_path = os.path.join(self.data_dir, f"{self.heat_id}.csv")
        with open(self.heat_csv_path, "w", newline="") as f:
            csv.writer(f).writerow(CSV_HEADERS)

        self.heat_start = timestamp
        self._event_ordinal = 0
        self.scrap_start = timestamp
        self._scrap_charging_confirmed = scrap_confirmed
        self.sponge_start = None
        self.tapping_start = None
        self.tilt_count = 0
        self._reset_furnace_watch()

        self.completed_events = []
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
            self.scrap_start = None
            self.sponge_start = timestamp
            self.state = self.STATE_SPONGE_CHARGING
            self.last_event_desc = f"{self.heat_id}: mid-process start during sponge charging"
            self._log_event("scrap_charging_unknown", None, None, note="app started during sponge charging")
            self._notify_event_start("sponge_charging", timestamp)
            self._request_frame("sponge_charging_start_midprocess", timestamp,
                                "mid-process recovery - sponge charging")
        else:
            self._notify_event_start("scrap_charging", timestamp)
            self._request_frame("scrap_charging_start", timestamp,
                                "new heat started - scrap charging")
        self._save_active_state()

    def _end_scrap_start_sponge(self, timestamp, note=""):
        self._log_event("scrap_charging", self.scrap_start, timestamp, note=note)
        self.sponge_start = timestamp
        self.state = self.STATE_SPONGE_CHARGING
        suffix = f" ({note})" if note else ""
        self.last_event_desc = f"{self.heat_id}: sponge charging started{suffix}"
        self._notify_event_start("sponge_charging", timestamp)
        self._request_frame("sponge_charging_start", timestamp,
                            f"scrap -> sponge ({note or 'sponge class confirmed'})")
        self._save_active_state()

    def _reset_furnace_watch(self):
        self.furnace_moving = False
        self.furnace_move_start = None
        self.last_tilt_end = None
        self.tap_candidate_active = False
        self.tap_candidate_start = None
        self.tap_candidate_deadline = None
        self.tap_candidate_opener = None
        self._tap_monitors = []
        self._tap_baselines = {}
        self.pending_tilt = None
        # NEW:
        self.tapping_requires_movement = False
        self.tapping_movement_seen = False

    def _configured_monitors(self):
        """Only this slot's own monitor. A PYRO change on a *different*
        furnace no longer confirms tapping - the manager guarantees this
        monitor belongs to the physical stream currently backing this slot."""
        return [self.temperature_monitor] if self.temperature_monitor is not None else []

    def _handle_sponge_movement(self, ids, timestamp):
        # 1. Class 36 (laddle_placed_fortapping) is a first-class tapping
        #    trigger: the ladle was physically placed, so tapping starts NOW.
        #    Checked before the candidate-window logic so a stray class 18
        #    that already opened a window cannot delay or backdate this.
        if CLS_LADDLE_PLACED_FORTAPPING in ids:
            self._begin_tapping_from_ladle(timestamp)
            return

        # 2. If a candidate window is open, ladle (25) confirms tapping.
        if self.tap_candidate_active:
            if CLS_LADDLE_MOVING_WITHMELTIRON in ids:
                self._confirm_tapping(
                    timestamp,
                    f"laddle_moving_withmeltiron (25) after {self.tap_candidate_opener}",
                )
                return
            if self._check_tapping_candidate(timestamp):
                return

        # 3. Furnace leaves normal -> start a movement cycle.
        if (ids & AWAY_FROM_NORMAL_CLASS_IDS) and not self.furnace_moving:
            self.furnace_moving = True
            self.furnace_move_start = timestamp
            self.last_event_desc = f"{self.heat_id}: furnace moved away from normal"

        # 4. Class 18 opens the 5-minute tapping candidate window.
        opener_hits = ids & TAPPING_CANDIDATE_OPENERS
        if opener_hits and not self.tap_candidate_active:
            opener_cls = sorted(opener_hits)[0]
            self._begin_tapping_candidate(timestamp, opener_cls)

        # 5. Class 1 (furnace_position_normal) ends the movement cycle.
        if (ids & FURNACE_NORMAL_CLASS_IDS) and self.furnace_moving:
            self._on_return_to_normal(timestamp)

    def _on_return_to_normal(self, timestamp):
        move_start = self.furnace_move_start or timestamp
        self.furnace_moving = False
        self.furnace_move_start = None

        if self.tap_candidate_active:
            # Tapping check still running: decide when it resolves.
            self.pending_tilt = (move_start, timestamp)
            self.last_event_desc = f"{self.heat_id}: furnace back to normal, tapping check still running"
            return
        self._count_tilt(move_start, timestamp)

    def _count_tilt(self, move_start, move_end):
        if self.last_tilt_end is not None and (move_start - self.last_tilt_end) < TILT_MIN_GAP:
            logger.info("%s: movement at %s within 45s of last tilt - ignored.", self.heat_id, move_start)
            self.last_event_desc = f"{self.heat_id}: movement within 45s of last tilt ignored"
            return
        self.tilt_count += 1
        self.last_tilt_end = move_end
        self._log_event("tilt", move_start, move_end, tilt_number=self.tilt_count)
        self.last_event_desc = f"{self.heat_id}: furnace back to normal - tilt #{self.tilt_count}"
        self._request_frame(f"tilt_{self.tilt_count}", move_end,
                            f"furnace movement resolved as tilt #{self.tilt_count}")
        self._save_active_state()

    def _begin_tapping_from_ladle(self, timestamp):
        """Class 36 (laddle_placed_fortapping) is a first-class tapping trigger.

        The ladle has been physically placed for tapping, so that moment IS
        the start of tapping:
          * no 90-second pre-roll backdate (unlike class 18's path),
          * no 5-minute candidate window / PYRO confirmation wait,
          * sponge charging is closed at this same timestamp.

        The heat then stays in TAPPING state until a normal end-of-tapping
        rule fires - class 1, the TAPPING_MAX_MINUTES cap, or the next scrap
        class. Any candidate window already open from class 18 is discarded.
        """
        self._enter_tapping(
            tapping_start_time=timestamp,
            sponge_end_time=timestamp,
            note="laddle_placed_fortapping (36)",
        )
        # NEW: tapping opened by the ladle only completes after we have
        # actually seen the furnace leave normal position (tilt / bottom
        # visible) and then come back.
        self.tapping_requires_movement = True
        self._save_active_state()

    def _begin_tapping_candidate(self, timestamp, opener_cls=None):
        self.tap_candidate_active = True
        self.tap_candidate_start = timestamp
        self.tap_candidate_deadline = timestamp + TAPPING_CANDIDATE_WINDOW

        if opener_cls == CLS_LADDLE_PLACED_FORTAPPING:
            self.tap_candidate_opener = f"laddle_placed_fortapping ({opener_cls})"
        else:
            self.tap_candidate_opener = (
                f"furnace_bottom_visiblecompletely "
                f"({opener_cls if opener_cls is not None else CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY})"
            )

        self._tap_monitors = self._configured_monitors()
        self._tap_baselines = {}
        for monitor in self._tap_monitors:
            sample = monitor.get_latest()
            if sample and not sample.get("is_stale", True) and sample.get("value") is not None:
                self._tap_baselines[id(monitor)] = (sample["value"], sample.get("seq"))

        self.last_event_desc = (
            f"{self.heat_id}: tapping candidate opened by "
            f"{self.tap_candidate_opener} - waiting for ladle (class 25) (5 min)"
        )
        self._request_frame(
            "tapping_candidate_opened", timestamp,
            f"{self.tap_candidate_opener} - tapping candidate window opened",
        )
        self._save_active_state()

    def _check_tapping_candidate(self, timestamp):
        """Checks if the candidate window has expired.

        PYRO data changes do NOT confirm tapping. Tapping confirmation after
        furnace_bottom_visiblecompletely (class 18) is strictly triggered
        by class 25 (laddle_moving_withmeltiron).
        """
        if not self.tap_candidate_active:
            return False
        if timestamp > self.tap_candidate_deadline:
            self._expire_tapping_candidate(timestamp)
            return False
        return False

    def _expire_tapping_candidate(self, timestamp):
        """5 minutes passed after class 18 with no class 25 -> the movement was
        only a TILT, not a tapping.

        Per the plant rule, this is the ONLY outcome when candidate opener is not
        confirmed by class 25 inside the 5-minute window.
        Count the tilt now:
          - If the furnace came back to normal (class 1) during the window,
            use that recorded span (pending_tilt).
          - Otherwise use opener start -> candidate deadline as the span,
            so the tilt is still logged even if class 1 never arrived.
        """
        pending = self.pending_tilt
        move_start = self.furnace_move_start
        deadline = self.tap_candidate_deadline or timestamp

        # Clear candidate state before counting so _count_tilt logs cleanly.
        self.tap_candidate_active = False
        self.tap_candidate_start = None
        self.tap_candidate_deadline = None
        self.tap_candidate_opener = None
        self._tap_monitors = []
        self._tap_baselines = {}
        self.pending_tilt = None

        # Also clear the "moving" flag: we've just resolved this movement.
        # Without this, a class-1 arriving AFTER expiry would fire
        # _on_return_to_normal and count a second tilt for the same movement.
        self.furnace_moving = False
        self.furnace_move_start = None

        if pending is not None:
            # Furnace returned to normal (class 1) during the window.
            self._count_tilt(*pending)
        elif move_start is not None:
            # No clear return during the window: use class-18 start -> deadline.
            self._count_tilt(move_start, deadline)
        else:
            self._save_active_state()

    def _confirm_tapping(self, end_timestamp, note):
        start = self.tap_candidate_start - TAPPING_PRE_ROLL      # 1.5 min before 18
        if self.sponge_start is not None and start < self.sponge_start:
            start = self.sponge_start                            # never earlier than sponge start
        if start > end_timestamp:
            start = end_timestamp
        self._enter_tapping(tapping_start_time=start, sponge_end_time=start, note=note)
        self._finalize_tapping(end_timestamp, reason=note)

    def _enter_tapping(
        self,
        tapping_start_time,
        sponge_end_time,
        note=""
    ):
        self._log_event(
            "sponge_charging",
            self.sponge_start,
            sponge_end_time,
            note=note
        )

        self.tapping_start = tapping_start_time
        self.state = self.STATE_TAPPING

        self._reset_furnace_watch()

        suffix = f" ({note})" if note else ""

        self.last_event_desc = (
            f"{self.heat_id}: tapping started{suffix}"
        )

        self._notify_event_start(
            "tapping",
            tapping_start_time
        )
        self._request_frame("tapping_start", tapping_start_time,
                            f"tapping started ({note or 'ladle confirmed'})")
        self._save_active_state()

    def _finalize_tapping(self, end_timestamp, reason="", auto_next=False):
        """Ends tapping and completes the heat.

        The recorded tapping end is pushed forward by TAPPING_END_EXTENSION so
        the CSV/SQL row reflects the real end of tapping rather than the
        instant class 1 (or the timeout) fired.

        auto_next=True: a new heat is started immediately in scrap charging,
        at the extended end time. Use this for the "class 9 detected" and
        "15-minute cap" paths. Leave False for the "scrap arrived during
        tapping" path, which starts its own new heat at the scrap timestamp.
        """
        extended_end = end_timestamp + TAPPING_END_EXTENSION

        self._request_frame(
            "tapping_end", end_timestamp,
            f"heat complete ({reason or 'normal position'})",
        )

        # Tapping row is written with the extended end time.
        self._log_event("tapping", self.tapping_start, extended_end, note=reason)

        self._update_index_row(
            self.heat_id, status="COMPLETE", end=extended_end,
            tilt_count=self.tilt_count,
        )

        heat_minutes = (
            round((extended_end - self.heat_start).total_seconds() / 60.0, 2)
            if self.heat_start else None
        )

        scrap_dur = sum(
            float(e["duration_minutes"]) for e in self.completed_events
            if e.get("event") == "scrap_charging" and e.get("duration_minutes")
        )
        sponge_dur = sum(
            float(e["duration_minutes"]) for e in self.completed_events
            if e.get("event") == "sponge_charging" and e.get("duration_minutes")
        )
        tapping_dur = sum(
            float(e["duration_minutes"]) for e in self.completed_events
            if e.get("event") == "tapping" and e.get("duration_minutes")
        )

        self._notify(
            "record_heat_completed",
            heat_id=self.heat_id,
            heat_number=self.heat_number,
            start_time=self.heat_start,
            end_time=extended_end,
            duration_minutes=heat_minutes,
            scrap_duration=round(scrap_dur, 2) if scrap_dur > 0 else None,
            sponge_duration=round(sponge_dur, 2) if sponge_dur > 0 else None,
            tapping_duration=round(tapping_dur, 2) if tapping_dur > 0 else None,
            tilt_count=self.tilt_count,
            status="COMPLETE",
            csv_file=os.path.basename(self.heat_csv_path) if self.heat_csv_path else None,
        )

        completed_heat_id = self.heat_id
        suffix = f" ({reason})" if reason else ""
        self.last_event_desc = (
            f"{completed_heat_id}: tapping ended, heat complete{suffix}"
        )
        self.state = self.STATE_IDLE
        self.tapping_start = None
        self._clear_active_state(completed_heat_id)

        if auto_next:
            # Start the next heat in scrap charging at the extended tap end.
            # This heat is *provisional* - it must see a real scrap-class detection
            # within SCRAP_MAX_MINUTES, otherwise _check_timeouts_locked aborts it.
            self._start_new_heat(extended_end, scrap_confirmed=False)

    # -----------------------------------------------------------------
    # CSV helpers
    # -----------------------------------------------------------------
    def _log_event(self, name, start, end, tilt_number="", note=""):
        self._touch_index_row()
        duration_min = round((end - start).total_seconds() / 60.0, 2) if start and end else ""
        row = [
            name,
            start.isoformat() if start else "",
            end.isoformat() if end else "",
            duration_min,
            tilt_number,
            note,
        ]
        if self.heat_csv_path:
            with open(self.heat_csv_path, "a", newline="") as f:
                csv.writer(f).writerow(row)

        event_dict = dict(zip(CSV_HEADERS, row))
        event_dict["event_ordinal"] = self._event_ordinal
        self.completed_events.append(event_dict)

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

    def _ensure_index_header(self):
        if not os.path.exists(self.index_csv) or os.path.getsize(self.index_csv) == 0:
            with open(self.index_csv, "w", newline="") as f:
                csv.writer(f).writerow(INDEX_HEADERS)
            return

        # Upgrade a pre-last_updated file: preserve existing rows, pad the
        # missing column with an empty string. Idempotent.
        with open(self.index_csv, "r", newline="") as f:
            rows = list(csv.reader(f))
        if not rows:
            with open(self.index_csv, "w", newline="") as f:
                csv.writer(f).writerow(INDEX_HEADERS)
            return

        old_header = rows[0]
        if "last_updated" in old_header:
            return   # already current

        new_rows = [INDEX_HEADERS]
        for old_row in rows[1:]:
            old_dict = dict(zip(old_header, old_row))
            new_rows.append([old_dict.get(h, "") for h in INDEX_HEADERS])

        with open(self.index_csv, "w", newline="") as f:
            csv.writer(f).writerows(new_rows)

        logger.info(
            "Index CSV upgraded: added 'last_updated' column (%d row(s) preserved).",
            len(new_rows) - 1,
        )

    def _load_last_heat_number(self):
        if not os.path.exists(self.index_csv):
            return 0
        with open(self.index_csv, "r", newline="") as f:
            reader = csv.DictReader(f)
            last_num = 0
            for row in reader:
                hid = row.get("heat_id", "")
                parts = hid.split("_")
                if len(parts) >= 2 and parts[1].isdigit():
                    num = int(parts[1])
                    if num > last_num:
                        last_num = num
            return last_num

    # -----------------------------------------------------------------
    # Active state persistence (JSON)
    # -----------------------------------------------------------------
    def _state_file_path(self, heat_id=None):
        hid = heat_id or self.heat_id
        if not hid:
            return None
        return os.path.join(self.data_dir, f"{hid}.state.json")

    def _save_active_state(self):
        """Atomically persist in-memory heat state to disk so crashes or
        restarts seamlessly restore exact stage, tilts, and flags without
        breaking an in-progress process."""
        path = self._state_file_path()
        if not path or not self.heat_id or self.state == self.STATE_IDLE:
            return

        payload = {
            "heat_id": self.heat_id,
            "heat_number": self.heat_number,
            "furnace_id": self.furnace_id,
            "state": self.state,
            "heat_start": self.heat_start.isoformat() if self.heat_start else None,
            "scrap_start": self.scrap_start.isoformat() if self.scrap_start else None,
            "sponge_start": self.sponge_start.isoformat() if self.sponge_start else None,
            "tapping_start": self.tapping_start.isoformat() if self.tapping_start else None,
            "tilt_count": self.tilt_count,
            "scrap_charging_confirmed": self._scrap_charging_confirmed,
            "furnace_moving": self.furnace_moving,
            "furnace_move_start": self.furnace_move_start.isoformat() if self.furnace_move_start else None,
            "last_tilt_end": self.last_tilt_end.isoformat() if self.last_tilt_end else None,
            "tap_candidate_active": self.tap_candidate_active,
            "tap_candidate_start": self.tap_candidate_start.isoformat() if self.tap_candidate_start else None,
            "tap_candidate_deadline": self.tap_candidate_deadline.isoformat() if self.tap_candidate_deadline else None,
            "tap_candidate_opener": self.tap_candidate_opener,
            "tapping_requires_movement": self.tapping_requires_movement,
            "tapping_movement_seen": self.tapping_movement_seen,
            "last_activefire_at": self.last_activefire_at.isoformat() if self.last_activefire_at else None,
            "last_event_desc": self.last_event_desc,
            "updated_at": datetime.datetime.now().isoformat(),
        }

        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            logger.exception("Failed to write active state to %s", path)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def _clear_active_state(self, heat_id=None):
        """Remove active state file when heat completes or aborts."""
        path = self._state_file_path(heat_id)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.exception("Failed to remove active state file %s", path)

    # -----------------------------------------------------------------
    # Startup resume
    # -----------------------------------------------------------------
    def _resume_incomplete_heat_if_recent(self):
        """On startup, look for the most recent IN_PROGRESS heat in the CSV
        index. Resume it iff its last_updated heartbeat is within
        RESUME_INCOMPLETE_HEAT_MAX_MINUTES. Older IN_PROGRESS heats are
        marked ABORTED (kept on disk for audit, never deleted).
        """
        if not os.path.exists(self.index_csv):
            return

        try:
            with open(self.index_csv, "r", newline="") as f:
                index_rows = list(csv.DictReader(f))
        except (OSError, csv.Error):
            logger.exception(
                "Startup: could not read index CSV %s - skipping resume.",
                self.index_csv,
            )
            return

        incomplete = [
            r for r in index_rows
            if (r.get("status") or "").strip().upper() == "IN_PROGRESS"
        ]
        if not incomplete:
            logger.info(
                "Startup: no IN_PROGRESS heat found in %s - starting fresh.",
                self.index_csv,
            )
            return

        logger.info(
            "Startup: found %d IN_PROGRESS heat(s) in %s - checking the most recent.",
            len(incomplete), self.index_csv,
        )

        def _row_last_touch(r):
            # Prefer the explicit heartbeat; fall back to start_time for rows
            # upgraded from an older index that predates last_updated.
            ts = (r.get("last_updated") or r.get("start_time") or "").strip()
            try:
                return datetime.datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                return datetime.datetime.min

        incomplete.sort(key=_row_last_touch, reverse=True)
        candidate = incomplete[0]
        stale = incomplete[1:]
        now = datetime.datetime.now()

        # Retire older IN_PROGRESS rows from earlier crashes.
        for r in stale:
            hid = r.get("heat_id")
            if not hid:
                continue
            logger.warning(
                "Startup: marking stale IN_PROGRESS heat %s as ABORTED (superseded "
                "by a more recent incomplete heat).", hid,
            )
            end_dt = _row_last_touch(r)
            self._update_index_row(
                hid, status="ABORTED",
                end=(end_dt if end_dt != datetime.datetime.min else None),
            )
            self._clear_active_state(hid)

        hid = candidate.get("heat_id")
        last_touch = _row_last_touch(candidate)

        if not hid or last_touch == datetime.datetime.min:
            if hid:
                logger.warning(
                    "Startup: last IN_PROGRESS heat %r has no parseable "
                    "last_updated/start_time - marking ABORTED.", hid,
                )
                self._update_index_row(hid, status="ABORTED")
                self._clear_active_state(hid)
            return

        age_minutes = (now - last_touch).total_seconds() / 60.0
        limit = getattr(config, "RESUME_INCOMPLETE_HEAT_MAX_MINUTES", 20)

        if limit <= 0 or age_minutes > limit:
            logger.info(
                "Startup: last IN_PROGRESS heat %s was last updated %.1f min "
                "ago (limit %.0f min) - marking ABORTED, not resuming.",
                hid, age_minutes, limit,
            )
            self._update_index_row(hid, status="ABORTED", end=last_touch)
            self._clear_active_state(hid)
            return

        # start_time is what the heat actually began at (used for the CSV /
        # duration), which is different from last_touch.
        start_dt = self._parse_iso_or_none(candidate.get("start_time")) or last_touch

        logger.warning(
            "Startup: RESUMING heat %s (heartbeat %.1f min ago, limit %.0f min).",
            hid, age_minutes, limit,
        )
        self._restore_heat_from_disk(index_rows, candidate, start_dt, last_touch)

    def _restore_heat_from_disk(self, index_rows, index_row, start_dt, last_touch):
        """Repopulate the state machine's live state from on-disk state JSON
        plus its per-heat CSV. Called only from
        _resume_incomplete_heat_if_recent (verified candidate)."""
        age_minutes = (datetime.datetime.now() - last_touch).total_seconds() / 60.0
        heat_id = index_row["heat_id"]
        csv_filename = index_row.get("csv_file") or f"{heat_id}.csv"
        csv_path = os.path.join(self.data_dir, csv_filename)

        if not os.path.exists(csv_path):
            logger.warning(
                "Startup: cannot resume %s - heat CSV %s is missing; marking "
                "ABORTED.", heat_id, csv_path,
            )
            self._update_index_row(heat_id, status="ABORTED", end=datetime.datetime.now())
            self._clear_active_state(heat_id)
            return

        try:
            with open(csv_path, "r", newline="") as f:
                events = list(csv.DictReader(f))
        except (OSError, csv.Error):
            logger.exception(
                "Startup: failed reading %s - marking ABORTED.", csv_path,
            )
            self._update_index_row(heat_id, status="ABORTED", end=datetime.datetime.now())
            self._clear_active_state(heat_id)
            return

        # --- Identity ------------------------------------------------------
        self.heat_id = heat_id
        self.heat_csv_path = csv_path
        self.heat_start = start_dt
        self._event_ordinal = len(events)   # next _log_event() writes row N+1

        # --- Movement / candidate cleanup -----------------------------------
        self._reset_furnace_watch()

        # --- Tilts ---------------------------------------------------------
        # The index row's tilt_count is only accurate at completion, so count
        # from the CSV instead.
        tilt_events = [e for e in events if e.get("event") == "tilt"]
        self.tilt_count = len(tilt_events)
        if tilt_events:
            self.last_tilt_end = self._parse_iso_or_none(tilt_events[-1].get("end_time"))

        # --- Restore completed_events for get_status() / UI -----------------
        self.completed_events = []
        for i, e in enumerate(events):
            d = dict(e)
            d["event_ordinal"] = i
            self.completed_events.append(d)

        # --- Check for active state JSON file ------------------------------
        state_file = self._state_file_path(heat_id)
        state_data = None
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
            except Exception:
                logger.exception(
                    "Startup: failed reading active state file %s - falling back to CSV.",
                    state_file,
                )

        if state_data and isinstance(state_data, dict):
            logger.info("Startup: restoring exact heat state from %s", state_file)
            saved_state = state_data.get("state")
            if saved_state in (self.STATE_SCRAP_CHARGING, self.STATE_SPONGE_CHARGING, self.STATE_TAPPING):
                self.state = saved_state

            self.scrap_start = self._parse_iso_or_none(state_data.get("scrap_start")) or (
                start_dt if self.state == self.STATE_SCRAP_CHARGING else None
            )
            self.sponge_start = self._parse_iso_or_none(state_data.get("sponge_start")) or self.sponge_start
            self.tapping_start = self._parse_iso_or_none(state_data.get("tapping_start")) or self.tapping_start

            if "tilt_count" in state_data:
                self.tilt_count = max(self.tilt_count, int(state_data["tilt_count"]))

            if "scrap_charging_confirmed" in state_data:
                self._scrap_charging_confirmed = bool(state_data["scrap_charging_confirmed"])
            elif self.state in (self.STATE_SPONGE_CHARGING, self.STATE_TAPPING):
                self._scrap_charging_confirmed = True
            else:
                self._scrap_charging_confirmed = not self._was_tap_end_initiated(index_rows, index_row)

            self.tapping_requires_movement = bool(state_data.get("tapping_requires_movement", False))
            self.tapping_movement_seen = bool(state_data.get("tapping_movement_seen", False))
            if state_data.get("last_tilt_end"):
                self.last_tilt_end = self._parse_iso_or_none(state_data.get("last_tilt_end")) or self.last_tilt_end
        else:
            # Fallback to CSV reconstruction:
            scrap_ev = next((e for e in events if e.get("event") == "scrap_charging"), None)
            sponge_ev = next((e for e in events if e.get("event") == "sponge_charging"), None)
            tap_ev = next((e for e in events if e.get("event") == "tapping"), None)
            scrap_unknown_ev = next((e for e in events if e.get("event") == "scrap_charging_unknown"), None)

            self.tapping_start = None

            if tap_ev:
                logger.warning(
                    "Startup: %s has a tapping event yet was IN_PROGRESS - "
                    "resuming in SPONGE_CHARGING.", heat_id,
                )
                self.state = self.STATE_SPONGE_CHARGING
                self.scrap_start = self._parse_iso_or_none(scrap_ev.get("start_time")) if scrap_ev else start_dt
                self.sponge_start = self._parse_iso_or_none(sponge_ev.get("start_time")) if sponge_ev else start_dt
                self._scrap_charging_confirmed = True

            elif sponge_ev:
                self.state = self.STATE_TAPPING
                self.scrap_start = self._parse_iso_or_none(scrap_ev.get("start_time")) if scrap_ev else start_dt
                self.sponge_start = self._parse_iso_or_none(scrap_ev.get("end_time")) if scrap_ev else start_dt
                self.tapping_start = (
                    self._parse_iso_or_none(sponge_ev.get("end_time"))
                    or self._parse_iso_or_none(sponge_ev.get("start_time"))
                    or start_dt
                )
                self._scrap_charging_confirmed = True

            elif scrap_ev:
                self.state = self.STATE_SPONGE_CHARGING
                self.scrap_start = self._parse_iso_or_none(scrap_ev.get("start_time")) or start_dt
                self.sponge_start = self._parse_iso_or_none(scrap_ev.get("end_time")) or start_dt
                self._scrap_charging_confirmed = True

            elif scrap_unknown_ev:
                self.state = self.STATE_SPONGE_CHARGING
                self.scrap_start = None
                self.sponge_start = start_dt
                self._scrap_charging_confirmed = True

            else:
                self.state = self.STATE_SCRAP_CHARGING
                self.scrap_start = start_dt
                self.sponge_start = None
                was_tap_initiated = self._was_tap_end_initiated(index_rows, index_row)
                self._scrap_charging_confirmed = not was_tap_initiated

        # --- Watchdog clock -------------------------------------------------
        # We can't know whether class 5 was seen while the app was down; a
        # fresh clock avoids an immediate false abort on the first frame back.
        # Suspend watchdog until CameraWorker actually connects.
        self._camera_connected = False
        self._camera_down_since = datetime.datetime.now()
        self.last_activefire_at = datetime.datetime.now()

        # --- Mid-process recovery is now moot -------------------------------
        self._startup_recovery_available = False

        self.last_event_desc = (
            f"RESUMED {heat_id} after restart "
            f"(last heartbeat {age_minutes:.1f} min ago, state={self.state}, "
            f"{len(events)} event(s) on disk)"
        )

        logger.warning(
            "Startup: RESUMED heat %s (heartbeat %.1f min old, state=%s, "
            "tilts=%d, events=%d, scrap_confirmed=%s).",
            heat_id, age_minutes, self.state, self.tilt_count,
            len(events), self._scrap_charging_confirmed,
        )

        # Immediately persist the restored state back to disk
        self._save_active_state()

    def _was_tap_end_initiated(self, index_rows, this_row):
        """True iff a COMPLETE row in the index has end_time == this row's
        start_time, i.e. this heat was auto-started at the moment the
        previous heat finished tapping (see _finalize_tapping -> auto_next).
        """
        my_start = (this_row.get("start_time") or "").strip()
        if not my_start:
            return False
        my_id = this_row.get("heat_id")
        for r in index_rows:
            if r.get("heat_id") == my_id:
                continue
            if (r.get("status") or "").strip().upper() != "COMPLETE":
                continue
            if (r.get("end_time") or "").strip() == my_start:
                return True
        return False

    @staticmethod
    def _parse_iso_or_none(s):
        if not s:
            return None
        try:
            return datetime.datetime.fromisoformat(str(s).strip())
        except (TypeError, ValueError):
            return None

    def _append_index_row(self, status="IN_PROGRESS", start=None):
        start_str = start.isoformat() if start else ""
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        row = {
            "heat_id":          self.heat_id,
            "start_time":       start_str,
            "end_time":         "",
            "duration_minutes": "",
            "tilt_count":       self.tilt_count,
            "status":           status,
            "csv_file":         (os.path.basename(self.heat_csv_path)
                                 if self.heat_csv_path else ""),
            "last_updated":     now_iso,
        }
        with open(self.index_csv, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=INDEX_HEADERS).writerow(row)

    def _update_index_row(self, heat_id, status=None, end=None, tilt_count=None):
        if not os.path.exists(self.index_csv):
            return
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        rows = []
        with open(self.index_csv, "r", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("heat_id") == heat_id:
                    if status is not None:
                        r["status"] = status
                    if end is not None:
                        r["end_time"] = end.isoformat()
                        if r.get("start_time"):
                            try:
                                s_dt = datetime.datetime.fromisoformat(r["start_time"])
                                dur = round((end - s_dt).total_seconds() / 60.0, 2)
                                r["duration_minutes"] = str(dur)
                            except ValueError:
                                pass
                    if tilt_count is not None:
                        r["tilt_count"] = str(tilt_count)
                    r["last_updated"] = now_iso
                rows.append(r)

        with open(self.index_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_HEADERS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in INDEX_HEADERS})

    def _remove_index_row(self, heat_id):
        if not os.path.exists(self.index_csv):
            return
        rows = []
        with open(self.index_csv, "r", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("heat_id") != heat_id:
                    rows.append(r)
        with open(self.index_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_HEADERS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in INDEX_HEADERS})

    def _touch_index_row(self, force=False):
        """Bump last_updated=now on the current heat's index row.

        Called from the per-frame timeout tick and from every event log, but
        throttled so it rewrites the index at most once every
        _index_touch_interval seconds. That is the heartbeat that tells a
        future restart "the app was alive at this timestamp".
        """
        if not self.heat_id or not os.path.exists(self.index_csv):
            return
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_index_touch_mono) < self._index_touch_interval:
            return
        self._last_index_touch_mono = now_mono

        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        rows = []
        try:
            with open(self.index_csv, "r", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("heat_id") == self.heat_id:
                        r["last_updated"] = now_iso
                    rows.append(r)
            with open(self.index_csv, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=INDEX_HEADERS)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in INDEX_HEADERS})
            self._save_active_state()
        except OSError:
            logger.exception("Could not touch index row for %s", self.heat_id)

    # -----------------------------------------------------------------
    # SQL recorder notification
    # -----------------------------------------------------------------
    def _notify(self, method_name, **kwargs):
        if not self.recorder:
            return
        if self.furnace_id is not None and "furnace_id" not in kwargs:
            kwargs["furnace_id"] = self.furnace_id
        try:
            fn = getattr(self.recorder, method_name, None)
            if fn:
                fn(**kwargs)
        except Exception:
            logger.exception("Failed to notify SQL recorder: %s(%s)", method_name, kwargs)

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
