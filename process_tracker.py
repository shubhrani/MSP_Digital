"""
process_tracker.py

Turns per-frame YOLO detections from best.pt into furnace process state:

    magnet_feeding_scrap     -> "scrap_charging"
    spange_feeder_engaged    -> "sponge_charging"
    furnace_position_normal  -> "idle"            (baseline / nothing active)
    furnace_position_tilding -> "tilting"          (a COUNTED event, 1-5x/heat)
    furnace_position_up      -> "tapping"          (heat-ending trigger)

Design notes (read this before tuning constants):

  * magnet_feeding_scrap, spange_feeder_engaged, furnace_position_normal and
    furnace_position_tilding go into a size-30 rolling window. Every time the
    window is full, whichever label has the most votes ("mode") becomes the
    smoothed current_state. This is exactly the "list of 30, max = current
    process" behaviour that was asked for, and it naturally self-heals: once
    a tilt finishes and normal/sponge frames start refilling the window, the
    vote flips back on its own with no extra "resume" logic needed.

  * furnace_position_up (tapping) is deliberately NOT part of that window.
    Tapping is the one event that must be caught fast and unambiguously --
    if it were mixed into a 30-frame vote sitting on top of several minutes
    of sponge-charging history, it would take a long time (or might never)
    win a majority before the pour is already over. Instead it uses its own
    short consecutive-frame confirmation (TAPPING_CONFIRM_FRAMES frames) to
    START, then a matching confirm streak of furnace_position_up dropping
    out to END it and close the heat. That gives tapping a real, live
    duration too (pours take a few minutes, same as the other two stages),
    rather than treating it as a single instantaneous marker.

  * furnace_position_tilding DOES sit in the window (as you specified). Each
    time the smoothed state transitions INTO "tilting" it counts as one tilt
    event for the current heat -- not once per frame.

  * Tuning depends on your actual inference frame rate. 30 frames at 1 fps is
    30 seconds of history; at 10 fps it's 3 seconds. If detections flap in
    and out of majority too quickly or too slowly, adjust WINDOW_SIZE,
    MIN_VOTES_TO_SWITCH and TAPPING_CONFIRM_FRAMES together, or throttle how
    often you call update() (see inference_loop.py).

Thread-safety: call update() from your inference thread and snapshot() /
history() from your web server thread(s). Both are guarded by one lock.
"""

from collections import deque, Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
import threading

# ---- class name -> internal state mapping ---------------------------------

WINDOW_CLASSES = {
    "magnet_feeding_scrap": "scrap_charging",
    "spange_feeder_engaged": "sponge_charging",
    "furnace_position_normal": "idle",
    "furnace_position_tilding": "tilting",
}
TAPPING_CLASS = "furnace_position_up"

RELEVANT_CLASSES = set(WINDOW_CLASSES) | {TAPPING_CLASS}

STAGE_STATES = ("scrap_charging", "sponge_charging")  # the 2 timed pre-tap stages

# ---- tunables ---------------------------------------------------------------

WINDOW_SIZE = 30            # size of the rolling detection list, as requested
MIN_VOTES_TO_SWITCH = 16    # simple majority of 30 required before switching state
TAPPING_CONFIRM_FRAMES = 5  # consecutive furnace_position_up frames to confirm tapping


@dataclass
class ProcessEvent:
    process: str
    start: datetime
    end: Optional[datetime] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.end is None:
            return None
        return (self.end - self.start).total_seconds()

    def to_dict(self) -> dict:
        return {
            "process": self.process,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "duration_seconds": self.duration_seconds,
        }


@dataclass
class Heat:
    heat_id: int
    started_at: datetime
    ended_at: Optional[datetime] = None
    tilt_count: int = 0
    events: List[ProcessEvent] = field(default_factory=list)

    def stage_total_seconds(self, stage: str, now: Optional[datetime] = None) -> float:
        """Sum duration across all events of `stage` (handles interruption by tilts)."""
        total = 0.0
        for e in self.events:
            if e.process == stage:
                end = e.end or now or datetime.now()
                total += (end - e.start).total_seconds()
        return total

    def to_dict(self) -> dict:
        now = datetime.now()
        return {
            "heat_id": self.heat_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "tilt_count": self.tilt_count,
            "scrap_charging_seconds": self.stage_total_seconds("scrap_charging", now),
            "sponge_charging_seconds": self.stage_total_seconds("sponge_charging", now),
            "events": [e.to_dict() for e in self.events],
        }


class ProcessTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._window: deque = deque(maxlen=WINDOW_SIZE)

        self._current_state = "idle"
        self._current_event: Optional[ProcessEvent] = None

        # active_stage persists across brief "tilting" / "idle" interruptions
        # so the UI can show one continuous timer for scrap/sponge charging.
        self._active_stage: Optional[str] = None
        self._active_stage_started_at: Optional[datetime] = None

        self._tapping_start_streak = 0
        self._tapping_end_streak = 0
        self._heat_counter = 0
        self._heat: Optional[Heat] = None
        self._history: List[Heat] = []

    # ---- call this once per processed frame --------------------------------

    def update(self, detected_class: Optional[str], confidence: float = 0.0,
               ts: Optional[datetime] = None) -> None:
        """
        detected_class: the single highest-priority relevant class name for this
        frame (e.g. from inference_loop.top_relevant_detection), or None if
        nothing relevant cleared the confidence threshold this frame.
        """
        ts = ts or datetime.now()
        with self._lock:
            self._handle_tapping(detected_class, ts)
            if self._current_state != "tapping":
                self._handle_window(detected_class, ts)

    # ---- read-only views for the web layer ----------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            now = datetime.now()
            return {
                "current_state": self._current_state,
                "active_stage": self._active_stage,
                "active_stage_elapsed_seconds": (
                    (now - self._active_stage_started_at).total_seconds()
                    if self._active_stage_started_at else 0
                ),
                "heat_id": self._heat.heat_id if self._heat else None,
                "heat_started_at": self._heat.started_at.isoformat() if self._heat else None,
                "tilt_count_this_heat": self._heat.tilt_count if self._heat else 0,
                "window_fill": len(self._window),
                "heat_events": [e.to_dict() for e in self._heat.events] if self._heat else [],
            }

    def history(self) -> List[dict]:
        with self._lock:
            return [h.to_dict() for h in self._history]

    # ---- internals ------------------------------------------------------------

    def _ensure_heat_started(self, ts: datetime) -> None:
        if self._heat is None:
            self._heat_counter += 1
            self._heat = Heat(heat_id=self._heat_counter, started_at=ts)

    def _handle_tapping(self, detected_class: Optional[str], ts: datetime) -> None:
        is_tapping_frame = detected_class == TAPPING_CLASS

        if self._current_state != "tapping":
            # Not tapping yet - watch for a confirm streak to START it.
            self._tapping_start_streak = self._tapping_start_streak + 1 if is_tapping_frame else 0
            if self._tapping_start_streak >= TAPPING_CONFIRM_FRAMES:
                self._ensure_heat_started(ts)
                self._close_current_event(ts)
                self._current_state = "tapping"
                self._active_stage = "tapping"
                self._active_stage_started_at = ts
                self._start_event("tapping", ts)
                self._tapping_start_streak = 0
                self._tapping_end_streak = 0
        else:
            # Already tapping - watch for a confirm streak of it dropping out
            # (furnace back down) to END the heat. Pouring genuinely takes a
            # few minutes, so this stays open the whole time furnace_position_up
            # keeps being detected, giving tapping a real, live duration too.
            self._tapping_end_streak = self._tapping_end_streak + 1 if not is_tapping_frame else 0
            if self._tapping_end_streak >= TAPPING_CONFIRM_FRAMES:
                self._close_current_event(ts)
                self._end_heat(ts)
                self._tapping_end_streak = 0

    def _handle_window(self, detected_class: Optional[str], ts: datetime) -> None:
        if detected_class in WINDOW_CLASSES:
            self._window.append(WINDOW_CLASSES[detected_class])
        # frames with nothing relevant simply aren't appended - they don't
        # push the vote toward any particular label.

        if len(self._window) < WINDOW_SIZE:
            return

        label, votes = Counter(self._window).most_common(1)[0]
        if votes < MIN_VOTES_TO_SWITCH:
            return
        if label != self._current_state:
            self._transition(label, ts)

    def _transition(self, new_state: str, ts: datetime) -> None:
        self._ensure_heat_started(ts)
        self._close_current_event(ts)

        self._current_state = new_state
        if new_state in STAGE_STATES:
            if self._active_stage != new_state:
                self._active_stage_started_at = ts
            self._active_stage = new_state
        if new_state == "tilting" and self._heat is not None:
            self._heat.tilt_count += 1

        self._start_event(new_state, ts)

    def _start_event(self, process: str, ts: datetime) -> None:
        self._current_event = ProcessEvent(process=process, start=ts)
        if self._heat is not None:
            self._heat.events.append(self._current_event)

    def _close_current_event(self, ts: datetime) -> None:
        if self._current_event is not None and self._current_event.end is None:
            self._current_event.end = ts

    def _end_heat(self, ts: datetime) -> None:
        if self._heat is not None:
            self._heat.ended_at = ts
            self._history.append(self._heat)
        self._heat = None
        self._current_state = "idle"
        self._current_event = None
        self._active_stage = None
        self._active_stage_started_at = None
        self._window.clear()
