"""
HeatStateMachine
----------------
Turns a stream of *confirmed* class-ID detections into the furnace process
timeline described by the plant: scrap charging -> sponge charging ->
tapping, plus a tilt counter for furnace movements that turn out NOT to be
tapping.

Process sequence (38-class model + InfluxDB PYRO temperature):

  Stage 1: Scrap Charging (Heat Start)
  16 magnet_moving_with_scrap / 17 magnet_feeding_scrap / 19 furnace_operation_scrapfeeding
    -> Scrap charging starts (new heat begins).
       If seen while previous heat is in TAPPING, that heat is closed out
       immediately and the new heat starts from this same moment.
    -> Maximum duration: 45 minutes. If no sponge class appears by 45 minutes,
       automatically transitions to sponge charging at scrap_start + 45 min.

  Stage 2: Sponge Iron Charging
  6 sponge_feeder_engaged / 10 sponge_feeder_onfurnace /
  35 magnet_moving_with_sponge / 37 magnet_feeding_sponge
    -> Scrap charging ends, sponge charging starts in the SAME heat.
    -> Reference duration: 90-120 minutes.

  Furnace Movement & Tapping vs. Tilt Decision:
  - Movement cycle: 2 (position up), 8 (bottom visible partially),
    18 (tilting), or 24 (bottom visible completely) starts a movement cycle
    (furnace_moving = True). Returning to 9 (normal) ends the movement cycle.
  - Tilt counting: Returning to normal counts as a tilt unless it occurs
    within 45 seconds of the previous counted tilt's end (TILT_MIN_GAP = 45s).
  - Tapping check: Only class 24 (furnace_bottom_visiblecompletely) opens a
    tapping candidate window lasting up to 5 minutes (TAPPING_CANDIDATE_WINDOW = 5m).
  - Tapping confirmation:
    * Ladle moving with melt iron (class 25) during an active check confirms tapping.
    * Or a PYRO value change on either furnace monitor confirms tapping.
    * Tapping start time is backdated with a 90-second pre-roll before class 24
      (tap_candidate_start - 90s, clamped to sponge_start).
    * When tapping is confirmed, the heat completes immediately.
  - Candidate expiration: If 5 minutes pass with no ladle or PYRO change, the candidate
    expires and any pending movement resolves as a tilt.

  Heat Completion (Tapping -> IDLE):
    - 9 (furnace_position_normal) is detected while TAPPING, or
    - 15 minutes have passed since tapping started (safety timeout cap), or
    - 16/17/19 (scrap feeding) detected (next heat begins immediately).

  Active-Fire Watchdog:
  3 furnace_operation_activefire -> Heartbeat signal; confirmed sightings
       reset the active fire timeout clock. If no active fire is
       seen for the configured timeout (5 min) during an active heat, the heat is aborted/purged.

  Auxiliary Events:
  21 human_carring_silicomanganese / 22 human_feeding_silicomanganese
    -> Logged as silicomanganese alloy additions in HeatEvents.
  36 magnet_moving_slag
    -> Logged as slag removal / deslagging in HeatEvents.
  28 furnace_operation_undermaintenance
    -> Logged in furnace status if maintenance occurs.
"""

import csv
import datetime
import logging
import os
import random
import threading

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Class IDs used by the logic (mapped to 38-class YOLO model)
# ---------------------------------------------------------------------------
CLS_NEGATIVE_PILLERS = 0
CLS_SPONGE_FEEDER_FARFROMFURNACE = 1
CLS_FURNACE_POSITION_UP = 2
CLS_FURNACE_ACTIVEFIRE = 3
CLS_NEGATIVE_SCRAPONGROUND = 4
CLS_NEGATIVE_OTHERFURNACES = 5
CLS_SPONGE_FEEDER_ENGAGED = 6
CLS_NEGATIVE_CRANEBEAM = 7
CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY = 8
CLS_FURNACE_NORMAL = 9
CLS_SPONGE_FEEDER_ON_FURNACE = 10
CLS_NEGATIVE_FAN = 11
CLS_NEGATIVE_FURNACESMOKE = 12
CLS_LADDLE_MOVING_EMPTY = 13
CLS_NEGATIVE_SUNLIGHT = 14
CLS_MAGNET_MOVING_IDLE = 15
CLS_MAGNET_MOVING_WITH_SCRAP = 16
CLS_MAGNET_FEEDING_SCRAP = 17
CLS_FURNACE_TILTING = 18
CLS_FURNACE_OPERATION_SCRAPFEEDING = 19
CLS_NEGATIVE_SLAG = 20
CLS_HUMAN_CARRYING_SILICOMANGANESE = 21
CLS_HUMAN_FEEDING_SILICOMANGANESE = 22
CLS_NEGATIVE_NOTVISIBLE = 23
CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY = 24
CLS_LADDLE_MOVING_WITHMELTIRON = 25
CLS_SPONGE_FEEDER_NOTENGAGED = 26
CLS_MAGNET_MOVING_WITHSHAKER = 27
CLS_FURNACE_OPERATION_UNDERMAINTENANCE = 28
CLS_COIL_POSITION_PLACEDNEARFURNACE = 29
CLS_NEGATIVE_RAMMINGMUSSCONTAINER = 30
CLS_NEGATIVE_MAGNETONGROUND = 31
CLS_FURMA_POSITION_PLACEDNEARFURNACE = 32
CLS_FURNACE_OPERATION_FURMAREPLACED = 33
CLS_NEGATIVE_HANGGINGCREATE = 34
CLS_MAGNET_MOVING_WITH_SPONGE = 35
CLS_MAGNET_MOVING_SLAG = 36
CLS_MAGNET_FEEDING_SPONGE = 37

# Logical groupings
SCRAP_CHARGING_CLASS_IDS = {
    CLS_MAGNET_MOVING_WITH_SCRAP,        # 16
    CLS_MAGNET_FEEDING_SCRAP,            # 17
    CLS_FURNACE_OPERATION_SCRAPFEEDING,  # 19
}

SPONGE_CHARGING_CLASS_IDS = {
    CLS_SPONGE_FEEDER_ENGAGED,           # 6
    CLS_SPONGE_FEEDER_ON_FURNACE,        # 10
    CLS_MAGNET_MOVING_WITH_SPONGE,       # 35
    CLS_MAGNET_FEEDING_SPONGE,          # 37
}

# Any of these classes indicate furnace movement away from normal
# position during SPONGE CHARGING. Temperature decides whether it is
# tapping or a tilt.
FURNACE_MOVEMENT_CLASS_IDS = {
    CLS_FURNACE_POSITION_UP,                # 2
    CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY,   # 8
    CLS_FURNACE_TILTING,                    # 18
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,  # 24
    CLS_LADDLE_MOVING_WITHMELTIRON,         # 25
}

FURNACE_NORMAL_CLASS_IDS = {
    CLS_FURNACE_NORMAL,                     # 9
}

ACTIVEFIRE_CLASS_IDS = {
    CLS_FURNACE_ACTIVEFIRE,                 # 3
}

# Classes the state machine actually reacts to. Passed to the debouncer so
# it only tracks streaks for classes that matter.
RELEVANT_CLASS_IDS = (
    SCRAP_CHARGING_CLASS_IDS
    | SPONGE_CHARGING_CLASS_IDS
    | FURNACE_MOVEMENT_CLASS_IDS
    | FURNACE_NORMAL_CLASS_IDS
    | ACTIVEFIRE_CLASS_IDS
    | {
        CLS_LADDLE_MOVING_EMPTY,                # 13
        CLS_HUMAN_CARRYING_SILICOMANGANESE,     # 21
        CLS_HUMAN_FEEDING_SILICOMANGANESE,      # 22
        CLS_FURNACE_OPERATION_UNDERMAINTENANCE, # 28
        CLS_MAGNET_MOVING_SLAG,                 # 36
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
                  "tilt_count", "status", "csv_file"]

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
    CLS_FURNACE_POSITION_UP,                # 2
    CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY,   # 8
    CLS_FURNACE_TILTING,                    # 18
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,  # 24
}
TILT_MIN_GAP = datetime.timedelta(seconds=45)
TAPPING_CANDIDATE_WINDOW = datetime.timedelta(minutes=5)
TAPPING_PRE_ROLL = datetime.timedelta(seconds=90)


class HeatStateMachine:
    STATE_IDLE = "IDLE"
    STATE_SCRAP_CHARGING = "SCRAP_CHARGING"
    STATE_SPONGE_CHARGING = "SPONGE_CHARGING"
    STATE_TAPPING = "TAPPING"

    RELEVANT_CLASS_IDS = RELEVANT_CLASS_IDS
    EXPECTED_DURATION_MIN = EXPECTED_DURATION_MIN

    def __init__(self, data_dir="data/heats", index_csv="data/heats_index.csv",
                 recorder=None, furnace_id=None, heat_id_prefix="Heat",
                 temperature_monitor=None, on_activefire_loss=None,
                 on_maintenance_detected=None):
        self.data_dir = data_dir
        self.index_csv = index_csv
        self.recorder = recorder
        self.furnace_id = furnace_id
        self.heat_id_prefix = heat_id_prefix
        self.temperature_monitor = temperature_monitor
        self._on_activefire_loss = on_activefire_loss
        self._on_maintenance_detected = on_maintenance_detected

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
        self.tap_candidate_start = None      # moment class 24 was confirmed
        self.tap_candidate_deadline = None
        self._tap_monitors = []
        self._tap_baselines = {}             # id(monitor) -> (value, seq)

        self.pending_tilt = None             # (move_start, normal_time) held while a tapping check runs

        self.last_activefire_at = datetime.datetime.now()

        self.completed_events = []
        self.last_event_desc = "Waiting for scrap charging to begin..."
        self.last_updated = None

        self._startup_recovery_available = True

        # Pending "capture this frame" requests for CameraWorker to drain on
        # its next frame. Populated by _request_frame() at every stage
        # boundary; drained atomically by get_pending_frame_requests().
        self._frame_requests = []

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    def process_detections(self, newly_confirmed_ids, timestamp=None):
        """Feed a set of newly-confirmed class IDs into the state machine."""
        timestamp = timestamp or datetime.datetime.now()
        newly_confirmed_ids = set(newly_confirmed_ids) if newly_confirmed_ids else set()

        with self._lock:
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
                # (no case for STATE_SPONGE_CHARGING — scrap during sponge
                # charging is now ignored, sponge keeps running)

            # Transition from scrap charging to sponge charging
            if (newly_confirmed_ids & SPONGE_CHARGING_CLASS_IDS) and self.state == self.STATE_SCRAP_CHARGING:
                self._end_scrap_start_sponge(timestamp)

            # Tapping completion (class 9 returns to normal)
            if self.state == self.STATE_TAPPING:
                if newly_confirmed_ids & FURNACE_NORMAL_CLASS_IDS:
                    self._finalize_tapping(timestamp, reason="furnace returned to normal")

            if self.state == self.STATE_SPONGE_CHARGING:
                self._handle_sponge_movement(newly_confirmed_ids, timestamp)

            # Maintenance detection (class 28 overrides everything)
            if CLS_FURNACE_OPERATION_UNDERMAINTENANCE in newly_confirmed_ids:
                self.last_event_desc = f"{self.heat_id or 'Furnace'}: furnace under maintenance"
                self._request_frame("furnace_undermaintenance", timestamp, "furnace operation undermaintenance (28)")
                if self._on_maintenance_detected:
                    self._on_maintenance_detected()

            # Auxiliary process events during an active heat
            if self.state != self.STATE_IDLE:
                if newly_confirmed_ids & {CLS_HUMAN_FEEDING_SILICOMANGANESE, CLS_HUMAN_CARRYING_SILICOMANGANESE}:
                    self._log_event("silicomanganese_addition", timestamp, timestamp, note="silicomanganese addition confirmed")
                    self._request_frame("silicomanganese_addition", timestamp, "human feeding/carrying silicomanganese (21/22)")
                if CLS_MAGNET_MOVING_SLAG in newly_confirmed_ids:
                    self._log_event("slag_removal", timestamp, timestamp, note="magnet moving slag")
                    self._request_frame("slag_removal", timestamp, "magnet moving slag (36)")

            self.last_updated = timestamp

    def check_timeouts(self, timestamp=None):
        """Checks timeout rules and ongoing movement watch temperature."""
        timestamp = timestamp or datetime.datetime.now()
        with self._lock:
            self._check_timeouts_locked(timestamp)

    def note_activefire_seen(self, timestamp=None):
        """Updates active-fire watchdog clock when class 3 is observed."""
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
        # Active-fire watchdog: a heat is running but class 3 hasn't been
        # confirmed for ACTIVEFIRE_TIMEOUT_MINUTES -> purge the heat.
        if self.state != self.STATE_IDLE and self.heat_id is not None:
            baseline = self.last_activefire_at or self.heat_start
            if baseline is not None and (timestamp - baseline) >= ACTIVEFIRE_TIMEOUT:
                self._abort_heat_no_activefire(timestamp)

        # 45-minute scrap charging maximum timeout: auto-transition to sponge
        if (self.state == self.STATE_SCRAP_CHARGING
                and self.scrap_start is not None
                and (timestamp - self.scrap_start) >= SCRAP_MAX_THRESHOLD):
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

        if self.state == self.STATE_SPONGE_CHARGING and self.tap_candidate_active:
            self._check_tapping_candidate(timestamp)

        # 15-minute tapping timeout cap: safety completion if class 9 was missed
        if (self.state == self.STATE_TAPPING
                and self.tapping_start is not None
                and (timestamp - self.tapping_start) >= TAPPING_MAX_THRESHOLD):
            self._finalize_tapping(
                timestamp,
                reason=f"tapping hit {TAPPING_MAX_MINUTES} min cap without furnace_position_normal",
            )

    def _abort_heat_no_activefire(self, timestamp):
        """Class 3 hasn't been seen for ACTIVEFIRE_TIMEOUT while this heat was in progress."""
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

        self.heat_id = None
        self.heat_csv_path = None
        self.heat_start = None
        self._event_ordinal = 0
        self.scrap_start = None
        self.sponge_start = None
        self.tapping_start = None
        self.tilt_count = 0
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

    def _start_new_heat(self, timestamp, mid_process_stage=None):
        self._startup_recovery_available = False
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

    def _end_scrap_start_sponge(self, timestamp, note=""):
        self._log_event("scrap_charging", self.scrap_start, timestamp, note=note)
        self.sponge_start = timestamp
        self.state = self.STATE_SPONGE_CHARGING
        suffix = f" ({note})" if note else ""
        self.last_event_desc = f"{self.heat_id}: sponge charging started{suffix}"
        self._notify_event_start("sponge_charging", timestamp)
        self._request_frame("sponge_charging_start", timestamp,
                            f"scrap -> sponge ({note or 'sponge class confirmed'})")

    def _reset_furnace_watch(self):
        self.furnace_moving = False
        self.furnace_move_start = None
        self.last_tilt_end = None
        self.tap_candidate_active = False
        self.tap_candidate_start = None
        self.tap_candidate_deadline = None
        self._tap_monitors = []
        self._tap_baselines = {}
        self.pending_tilt = None

    def _configured_monitors(self):
        """Only this slot's own monitor. A PYRO change on a *different*
        furnace no longer confirms tapping - the manager guarantees this
        monitor belongs to the physical stream currently backing this slot."""
        return [self.temperature_monitor] if self.temperature_monitor is not None else []

    def _handle_sponge_movement(self, ids, timestamp):
        # 1. Open tapping check: ladle (25) has priority over PYRO.
        if self.tap_candidate_active:
            if CLS_LADDLE_MOVING_WITHMELTIRON in ids:
                self._confirm_tapping(
                    timestamp,
                    "laddle_moving_withmeltiron (25) after furnace_bottom_visiblecompletely (24)")
                return
            if self._check_tapping_candidate(timestamp):
                return

        # 2. Furnace leaves normal -> start a movement cycle.
        if (ids & AWAY_FROM_NORMAL_CLASS_IDS) and not self.furnace_moving:
            self.furnace_moving = True
            self.furnace_move_start = timestamp
            self.last_event_desc = f"{self.heat_id}: furnace moved away from normal"

        # 3. Class 24 (already debounced) opens the tapping check. Ignored if one is running.
        if CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY in ids and not self.tap_candidate_active:
            self._begin_tapping_candidate(timestamp)

        # 4. Class 9 (already debounced) ends the movement cycle.
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

    def _begin_tapping_candidate(self, timestamp):
        self.tap_candidate_active = True
        self.tap_candidate_start = timestamp
        self.tap_candidate_deadline = timestamp + TAPPING_CANDIDATE_WINDOW
        self._tap_monitors = self._configured_monitors()
        self._tap_baselines = {}
        for monitor in self._tap_monitors:            # record PYRO at the moment 24 was seen
            sample = monitor.get_latest()
            if sample and not sample.get("is_stale", True) and sample.get("value") is not None:
                self._tap_baselines[id(monitor)] = (sample["value"], sample.get("seq"))
        self.last_event_desc = f"{self.heat_id}: bottom fully visible - checking for tapping (5 min)"
        self._request_frame("tapping_candidate_opened", timestamp,
                            "furnace_bottom_visiblecompletely (24) - tapping candidate window opened")

    def _check_tapping_candidate(self, timestamp):
        """True if tapping was confirmed by this call."""
        if not self.tap_candidate_active:
            return False
        if timestamp > self.tap_candidate_deadline:
            self._expire_tapping_candidate(timestamp)
            return False
        for monitor in self._tap_monitors:
            field_name = getattr(monitor, "field", "PYRO")
            sample = monitor.get_latest()
            if not sample or sample.get("is_stale", True) or sample.get("value") is None:
                continue
            base = self._tap_baselines.get(id(monitor))
            if base is None:                            # nothing usable when 24 was seen
                self._tap_baselines[id(monitor)] = (sample["value"], sample.get("seq"))
                continue
            if sample["value"] != base[0]:
                self._confirm_tapping(
                    timestamp,
                    f"{field_name} changed {base[0]} -> {sample['value']} after "
                    "furnace_bottom_visiblecompletely (24)")
                return True
        return False

    def _expire_tapping_candidate(self, timestamp):
        """5 minutes passed after class 24 with no class 25 and no PYRO
        change -> the movement was only a TILT, not a tapping.

        Per the plant rule, this is the ONLY outcome when class 24 is not
        confirmed by class 25 or a PYRO change inside the 5-minute window.
        Count the tilt now:
          - If the furnace came back to normal (class 9) during the window,
            use that recorded span (pending_tilt).
          - Otherwise use class-24 start -> candidate deadline as the span,
            so the tilt is still logged even if class 9 never arrived.
        """
        pending = self.pending_tilt
        move_start = self.furnace_move_start
        deadline = self.tap_candidate_deadline or timestamp

        # Clear candidate state before counting so _count_tilt logs cleanly.
        self.tap_candidate_active = False
        self.tap_candidate_start = None
        self.tap_candidate_deadline = None
        self._tap_monitors = []
        self._tap_baselines = {}
        self.pending_tilt = None

        # Also clear the "moving" flag: we've just resolved this movement.
        # Without this, a class-9 arriving AFTER expiry would fire
        # _on_return_to_normal and count a second tilt for the same movement.
        self.furnace_moving = False
        self.furnace_move_start = None

        if pending is not None:
            # Furnace returned to normal (class 9) during the window.
            self._count_tilt(*pending)
        elif move_start is not None:
            # No clear return during the window: use class-24 start -> deadline.
            self._count_tilt(move_start, deadline)

    def _confirm_tapping(self, end_timestamp, note):
        start = self.tap_candidate_start - TAPPING_PRE_ROLL      # 1.5 min before 24
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
                            f"tapping started ({note or 'PYRO/ladle confirmed'})")

    def _finalize_tapping(self, end_timestamp, reason=""):
        """Ends tapping and completes the heat."""
        self._request_frame("tapping_end", end_timestamp,
                            f"heat complete ({reason or 'normal position'})")
        self._log_event("tapping", self.tapping_start, end_timestamp, note=reason)
        self._update_index_row(self.heat_id, status="COMPLETE", end=end_timestamp,
                                tilt_count=self.tilt_count)

        heat_minutes = (round((end_timestamp - self.heat_start).total_seconds() / 60.0, 2)
                        if self.heat_start else None)

        scrap_dur = sum(float(e["duration_minutes"]) for e in self.completed_events
                        if e.get("event") == "scrap_charging" and e.get("duration_minutes"))
        sponge_dur = sum(float(e["duration_minutes"]) for e in self.completed_events
                         if e.get("event") == "sponge_charging" and e.get("duration_minutes"))
        tapping_dur = sum(float(e["duration_minutes"]) for e in self.completed_events
                          if e.get("event") == "tapping" and e.get("duration_minutes"))

        self._notify("record_heat_completed",
                     heat_id=self.heat_id,
                     heat_number=self.heat_number,
                     start_time=self.heat_start,
                     end_time=end_timestamp,
                     duration_minutes=heat_minutes,
                     scrap_duration=round(scrap_dur, 2) if scrap_dur > 0 else None,
                     sponge_duration=round(sponge_dur, 2) if sponge_dur > 0 else None,
                     tapping_duration=round(tapping_dur, 2) if tapping_dur > 0 else None,
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

    def _append_index_row(self, status="IN_PROGRESS", start=None):
        start_str = start.isoformat() if start else ""
        row = [self.heat_id, start_str, "", "", self.tilt_count, status,
               os.path.basename(self.heat_csv_path) if self.heat_csv_path else ""]
        with open(self.index_csv, "a", newline="") as f:
            csv.writer(f).writerow(row)

    def _update_index_row(self, heat_id, status=None, end=None, tilt_count=None):
        if not os.path.exists(self.index_csv):
            return
        rows = []
        with open(self.index_csv, "r", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            for r in reader:
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
                rows.append(r)

        with open(self.index_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def _remove_index_row(self, heat_id):
        if not os.path.exists(self.index_csv):
            return
        rows = []
        with open(self.index_csv, "r", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            for r in reader:
                if r.get("heat_id") != heat_id:
                    rows.append(r)
        with open(self.index_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

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
