"""Regression tests for the plant heat sequence (43-class model + InfluxDB temperature).

Lifecycle:
    scrap (24, 21, 37) -> sponge (4, 15, 6, 19, 27, 2 or 45-min cap) -> tapping (class 18 + ladle 25, or class 36) -> complete

Key rules:
    - 24, 21, 37 start scrap charging.
    - 4, 15, 6, 19, 27, 2 transition scrap to sponge charging.
    - Scrap charging auto-transitions to sponge charging at scrap_start + 45 minutes if no sponge class is detected.
    - During sponge charging:
        * Movement cycle: 34, 35, 17, 18 start movement; class 1 ends movement.
        * Tilts count on return to normal (class 1) with a 45s minimum gap between tilts.
        * Tapping candidate: opened strictly by class 18 (5-minute window).
        * Ladle 25 during candidate confirms tapping immediately (PYRO does not confirm tapping).
        * Tapping start time = class 18 time - 90s pre-roll (clamped to sponge start).
        * Tapping confirmation completes the heat immediately.
        * Expired candidate (5 min without ladle 25) resolves pending movement as tilt.
    - Active-fire watchdog purges heats where class 5 has been missing for timeout.
    - Mid-process startup recovery supports sponge classes and movement classes.
"""
import csv
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from state_machine import (
    HeatStateMachine,
    CLS_FURNACE_POSITION_UP,
    CLS_FURNACE_ACTIVEFIRE,
    CLS_SPONGE_FEEDER_ENGAGED,
    CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY,
    CLS_FURNACE_NORMAL,
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_SPONGE_FEEDER_INITIALHEAT,
    CLS_SPONGE_FEEDER_STAND,
    CLS_MAGNET_MOVING_WITH_SCRAP,
    CLS_MAGNET_FEEDING_SCRAP,
    CLS_FURNACE_TILTING,
    CLS_FURNACE_OPERATION_SCRAPFEEDING,
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,
    CLS_LADDLE_MOVING_WITHMELTIRON,
    CLS_LADDLE_PLACED_FORTAPPING,
    CLS_HUMAN_FEEDING_SCRAP,
    CLS_MAGNET_MOVING_WITH_SPONGE,
    CLS_MAGNET_FEEDING_SPONGE,
    SCRAP_CHARGING_CLASS_IDS,
    SPONGE_CHARGING_CLASS_IDS,
    UNDERMAINTENANCE_CLASS_IDS,
    CLS_COIL_POSITION_PLACEDNEARFURNACE,
    CLS_FURNACE_OPERATION_PREHEATING,
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
    CLS_FURMA_POSITION_PLACEDNEARFURNACE,
    TAPPING_END_EXTENSION,
)

TMP_DIR = os.path.join(os.path.dirname(__file__), "_tmp_data")
BASE = datetime.datetime(2026, 8, 24, 10, 0, 0)


class MockTemperatureMonitor:
    def __init__(self, temp=1560.0):
        self.temp = None
        self.seq = 0
        self.history = []
        self.is_stale = False
        if temp is not None:
            self.set_temperature(temp)

    def get_temperature(self):
        if self.is_stale:
            return None
        return self.temp

    def get_latest(self):
        return {
            "value": self.temp,
            "seq": self.seq,
            "is_stale": self.is_stale,
            "connected": True,
            "last_error": None,
            "data_age_seconds": 99.0 if self.is_stale else 0.0,
        }

    def get_recent_samples(self, seconds=None):
        return list(self.history)

    def set_temperature(self, new_temp):
        self.seq += 1
        self.temp = new_temp
        self.history.append({
            "seq": self.seq,
            "value": new_temp,
            "source_timestamp": None,
            "received_monotonic": 0,
        })


def t(m):
    return BASE + datetime.timedelta(minutes=m)


def ts(s):
    return BASE + datetime.timedelta(seconds=s)


def fresh_machine(temp_monitor=None, on_activefire_loss=None):
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    return HeatStateMachine(
        data_dir=os.path.join(TMP_DIR, "heats"),
        index_csv=os.path.join(TMP_DIR, "heats_index.csv"),
        temperature_monitor=temp_monitor or MockTemperatureMonitor(1560.0),
        on_activefire_loss=on_activefire_loss,
    )


def hb(sm, m):
    sm.note_activefire_seen(t(m))


def keep_alive(sm, start, end, step=2):
    m = start
    while m <= end:
        hb(sm, m)
        m += step


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_scrap_charging_class_ids():
    """Verify classes in SCRAP_CHARGING_CLASS_IDS all start scrap charging."""
    for cls_id in (CLS_MAGNET_FEEDING_SCRAP, CLS_FURNACE_OPERATION_SCRAPFEEDING, CLS_HUMAN_FEEDING_SCRAP):
        sm = fresh_machine()
        assert sm.state == sm.STATE_IDLE
        sm.process_detections({cls_id}, t(0))
        assert sm.state == sm.STATE_SCRAP_CHARGING
        assert sm.scrap_start == t(0)
    print("test_scrap_charging_class_ids passed")


def test_sponge_charging_class_ids():
    """Verify classes in SPONGE_CHARGING_CLASS_IDS transition scrap to sponge charging."""
    for cls_id in (CLS_SPONGE_FEEDER_ENGAGED, CLS_SPONGE_FEEDER_ON_FURNACE,
                   CLS_MAGNET_FEEDING_SPONGE):
        sm = fresh_machine()
        sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
        assert sm.state == sm.STATE_SCRAP_CHARGING
        keep_alive(sm, 2, 10)
        sm.process_detections({cls_id}, t(10))
        assert sm.state == sm.STATE_SPONGE_CHARGING
        assert sm.sponge_start == t(10)
    print("test_sponge_charging_class_ids passed")


def test_scrap_45min_automatic_timeout():
    """Scrap charging automatically transitions to sponge charging at scrap_start + 45 min."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    assert sm.state == sm.STATE_SCRAP_CHARGING

    keep_alive(sm, 5, 44)
    sm.check_timeouts(t(44))
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # At 45 minutes: auto transition
    hb(sm, 45)
    sm.check_timeouts(t(45))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.sponge_start == t(45)

    events = read_csv(sm.heat_csv_path)
    scrap_ev = [e for e in events if e["event"] == "scrap_charging"][0]
    assert float(scrap_ev["duration_minutes"]) == 45.0
    print("test_scrap_45min_automatic_timeout passed")


def test_tilt_counting_2_18_9():
    """Tilt counting: 2 -> 18 -> 9 counts as ONE tilt, only when returning to normal."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    hb(sm, 23)
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.tilt_count == 0

    # 10:25:00: Furnace moves up (class 2)
    sm.process_detections({CLS_FURNACE_POSITION_UP}, t(25))
    assert sm.furnace_moving
    assert sm.furnace_move_start == t(25)
    assert sm.tilt_count == 0

    # 10:26:00: Furnace tilts (class 18) - still moving, same movement cycle
    sm.process_detections({CLS_FURNACE_TILTING}, t(26))
    assert sm.furnace_moving
    assert sm.furnace_move_start == t(25)
    assert sm.tilt_count == 0

    # 10:27:00: Furnace returns to normal (class 9) -> tilt counted!
    sm.process_detections({CLS_FURNACE_NORMAL}, t(27))
    assert not sm.furnace_moving
    assert sm.tilt_count == 1
    assert sm.last_tilt_end == t(27)
    assert sm.state == sm.STATE_SPONGE_CHARGING

    events = read_csv(sm.heat_csv_path)
    tilts = [e for e in events if e["event"] == "tilt"]
    assert len(tilts) == 1
    assert tilts[0]["tilt_number"] == "1"
    assert tilts[0]["start_time"] == t(25).isoformat()
    assert tilts[0]["end_time"] == t(27).isoformat()
    print("test_tilt_counting_2_18_9 passed")


def test_tilt_45_second_gap():
    """A movement starting less than 45s after the last tilt ended is ignored."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    hb(sm, 23)

    # First tilt: 10:25:00 to 10:26:00
    sm.process_detections({CLS_FURNACE_TILTING}, ts(25 * 60))
    sm.process_detections({CLS_FURNACE_NORMAL}, ts(26 * 60))
    assert sm.tilt_count == 1
    assert sm.last_tilt_end == ts(26 * 60)

    # Second movement starts 30 seconds after last tilt ended (< 45s gap)
    sm.process_detections({CLS_FURNACE_TILTING}, ts(26 * 60 + 30))
    assert sm.furnace_moving
    # Returns to normal at +40s
    sm.process_detections({CLS_FURNACE_NORMAL}, ts(26 * 60 + 40))
    assert not sm.furnace_moving
    # Must be ignored: tilt count remains 1 and last_tilt_end is unchanged
    assert sm.tilt_count == 1
    assert sm.last_tilt_end == ts(26 * 60)

    # Third movement starts 50 seconds after last tilt ended (>= 45s gap)
    sm.process_detections({CLS_FURNACE_TILTING}, ts(26 * 60 + 50))
    assert sm.furnace_moving
    sm.process_detections({CLS_FURNACE_NORMAL}, ts(27 * 60))
    assert not sm.furnace_moving
    # Must be counted: tilt count becomes 2
    assert sm.tilt_count == 2
    assert sm.last_tilt_end == ts(27 * 60)
    print("test_tilt_45_second_gap passed")


def test_bug_case_old_tilt_followed_by_tapping_much_later():
    """Bug fix verification:
    An old tilt followed by tapping much later now gives:
    tapping_start = (24 time - 1.5 min), tapping_end = PYRO change time.
    Sponge ends at tapping_start, and old tilt remains counted.
    """
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    # Tilt occurs at 10:30:00 -> 10:31:00
    keep_alive(sm, 21, 29)
    sm.process_detections({CLS_FURNACE_TILTING}, t(30))
    sm.process_detections({CLS_FURNACE_NORMAL}, t(31))
    assert sm.tilt_count == 1
    assert not sm.furnace_moving
    assert not sm.tap_candidate_active

    # Hours pass in sponge charging (active-fire keeps it alive)
    keep_alive(sm, 32, 120, step=2)

    # At 12:00:00 (t(120)), class 24 confirms bottom completely visible
    t24 = t(120)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t24)
    assert sm.tap_candidate_active
    assert sm.tap_candidate_start == t24
    assert sm.furnace_moving

    # At 12:01:00 (t(121)), PYRO changes 1560 -> 1572 -> must NOT confirm tapping
    temp_mon.set_temperature(1572.0)
    t_pyro = t(121)
    sm.check_timeouts(t_pyro)
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.tap_candidate_active

    # Class 25 (ladle) confirms tapping at 12:01:00
    sm.process_detections({CLS_LADDLE_MOVING_WITHMELTIRON}, t_pyro)

    # Tapping confirmed and heat finalized immediately
    assert sm.state == sm.STATE_IDLE
    assert sm.tilt_count == 1  # Old tilt remains counted!

    events = read_csv(sm.heat_csv_path)
    tilts = [e for e in events if e["event"] == "tilt"]
    sponge_evs = [e for e in events if e["event"] == "sponge_charging"]
    tapping_evs = [e for e in events if e["event"] == "tapping"]

    assert len(tilts) == 1
    assert len(sponge_evs) == 1
    assert len(tapping_evs) == 1

    expected_tapping_start = t24 - datetime.timedelta(seconds=90)  # 11:58:30
    assert sponge_evs[0]["end_time"] == expected_tapping_start.isoformat()
    assert tapping_evs[0]["start_time"] == expected_tapping_start.isoformat()
    assert tapping_evs[0]["end_time"] == (t_pyro + TAPPING_END_EXTENSION).isoformat()

    # Duration of tapping: 2.5 min + 1.25 min extension = 3.75 minutes
    assert float(tapping_evs[0]["duration_minutes"]) == 3.75
    print("test_bug_case_old_tilt_followed_by_tapping_much_later passed")


def test_expired_tapping_candidate():
    """Class 24, then 5 minutes with no change, counts as a tilt."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    # At 10:30:00, class 24 appears
    t24 = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t24)
    assert sm.tap_candidate_active
    assert sm.furnace_moving

    # At 10:31:00, class 9 appears (furnace returns to normal)
    t_norm = t(31)
    sm.process_detections({CLS_FURNACE_NORMAL}, t_norm)
    assert not sm.furnace_moving
    # Tapping check is still running; tilt is pending
    assert sm.tap_candidate_active
    assert sm.pending_tilt == (t24, t_norm)
    assert sm.tilt_count == 0

    # Keep active fire alive while tapping candidate window ticks down
    hb(sm, 32)
    hb(sm, 34)

    # Advance beyond 5-minute candidate window without PYRO change
    t_expire = t24 + datetime.timedelta(minutes=5, seconds=1)
    sm.check_timeouts(t_expire)

    # Candidate expired -> tilt is counted!
    assert not sm.tap_candidate_active
    assert sm.pending_tilt is None
    assert sm.tilt_count == 1
    assert sm.state == sm.STATE_SPONGE_CHARGING

    events = read_csv(sm.heat_csv_path)
    tilts = [e for e in events if e["event"] == "tilt"]
    assert len(tilts) == 1
    assert tilts[0]["start_time"] == t24.isoformat()
    assert tilts[0]["end_time"] == t_norm.isoformat()
    print("test_expired_tapping_candidate passed")


def test_expired_tapping_candidate_without_class_9():
    """Class 24, no class 9 and no PYRO/ladle within 5 min -> counts as tilt from class 24 to deadline."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    # At 10:30:00, class 24 appears
    t24 = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t24)
    assert sm.tap_candidate_active
    assert sm.furnace_moving
    assert sm.pending_tilt is None

    # Keep active fire alive while tapping candidate window ticks down (no class 9)
    hb(sm, 32)
    hb(sm, 34)

    # Advance beyond 5-minute candidate window without PYRO change or class 9
    t_expire = t24 + datetime.timedelta(minutes=5, seconds=1)
    sm.check_timeouts(t_expire)

    # Candidate expired -> tilt is counted!
    assert not sm.tap_candidate_active
    assert not sm.furnace_moving
    assert sm.furnace_move_start is None
    assert sm.pending_tilt is None
    assert sm.tilt_count == 1
    assert sm.state == sm.STATE_SPONGE_CHARGING

    events = read_csv(sm.heat_csv_path)
    tilts = [e for e in events if e["event"] == "tilt"]
    assert len(tilts) == 1
    deadline = t24 + datetime.timedelta(minutes=5)
    assert tilts[0]["start_time"] == t24.isoformat()
    assert tilts[0]["end_time"] == deadline.isoformat()

    # If class 9 arrives AFTER expiry, it should NOT count a duplicate tilt
    sm.process_detections({CLS_FURNACE_NORMAL}, t(36))
    assert sm.tilt_count == 1
    events2 = read_csv(sm.heat_csv_path)
    tilts2 = [e for e in events2 if e["event"] == "tilt"]
    assert len(tilts2) == 1
    print("test_expired_tapping_candidate_without_class_9 passed")


def test_pyro_change_does_not_confirm_tapping():
    """A PYRO change after class 18 must NOT confirm tapping."""
    own_temp = MockTemperatureMonitor(1560.0)
    own_temp.field = "PYRO4/1"
    sm = fresh_machine(own_temp)

    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    # Class 18 opens tapping check
    t18 = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t18)
    assert sm.tap_candidate_active

    # Own furnace PYRO changes at 10:31:00 -> must NOT confirm tapping
    own_temp.set_temperature(1568.0)
    t_change = t(31)
    sm.check_timeouts(t_change)

    # State remains SPONGE_CHARGING, candidate remains active, no tapping event
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.tap_candidate_active
    events = read_csv(sm.heat_csv_path)
    taps = [e for e in events if e["event"] == "tapping"]
    assert len(taps) == 0

    # Advance past 5 minutes without class 25 -> candidate window expires as a tilt
    sm.check_timeouts(t18 + datetime.timedelta(minutes=5, seconds=1))
    assert not sm.tap_candidate_active
    assert sm.tilt_count == 1
    print("test_pyro_change_does_not_confirm_tapping passed")


def test_activefire_loss_callback():
    """Active fire loss aborts the heat and fires the on_activefire_loss callback."""
    loss_called = []
    def on_loss():
        loss_called.append(True)

    sm = fresh_machine(on_activefire_loss=on_loss)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # Timeout passes without active fire
    timeout_mins = config.ACTIVEFIRE_TIMEOUT_MINUTES + 0.5
    sm.check_timeouts(t(timeout_mins))
    assert sm.state == sm.STATE_IDLE
    assert len(loss_called) == 1
    print("test_activefire_loss_callback passed")


def test_other_triggers_class_25_ladle():
    """Ladle detection (25) after class 18 confirms tapping with no PYRO change."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    t18 = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t18)
    assert sm.tap_candidate_active

    # Ladle confirmed at 10:31:00
    t25 = t(31)
    sm.process_detections({CLS_LADDLE_MOVING_WITHMELTIRON}, t25)

    assert sm.state == sm.STATE_IDLE
    events = read_csv(sm.heat_csv_path)
    taps = [e for e in events if e["event"] == "tapping"]
    assert len(taps) == 1
    assert "laddle_moving_withmeltiron (25) after furnace_bottom_visiblecompletely (18)" in taps[0]["note"]
    print("test_other_triggers_class_25_ladle passed")


def test_edge_cases_repeat_24_and_class_25_alone():
    """Repeat 24 is ignored while check is running; 25 alone does nothing."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 24)
    # 1. Class 25 ALONE during sponge charging does not start movement or tapping
    sm.process_detections({CLS_LADDLE_MOVING_WITHMELTIRON}, t(25))
    assert not sm.furnace_moving
    assert not sm.tap_candidate_active
    assert sm.state == sm.STATE_SPONGE_CHARGING

    keep_alive(sm, 26, 29)
    # 2. Class 24 opens check at 10:30:00
    t24_first = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t24_first)
    assert sm.tap_candidate_active
    assert sm.tap_candidate_start == t24_first

    # 3. Repeat class 24 at 10:31:00 while candidate is active is ignored
    t24_second = t(31)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t24_second)
    assert sm.tap_candidate_active
    assert sm.tap_candidate_start == t24_first  # Not overwritten
    print("test_edge_cases_repeat_24_and_class_25_alone passed")


def test_tapping_15_minute_timeout_cap():
    """Tapping safely caps and completes at 15 minutes if normal position is missed."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    heat_1 = sm.heat_id
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    # Manually transition to tapping to test the 15-minute cap watchdog
    sm._enter_tapping(tapping_start_time=t(30), sponge_end_time=t(30), note="test")
    assert sm.state == sm.STATE_TAPPING

    keep_alive(sm, 31, 45)
    sm.check_timeouts(t(45))  # exactly 15 minutes
    # With auto_next=True, a new heat starts immediately in SCRAP_CHARGING
    assert sm.heat_id != heat_1
    assert sm.state == sm.STATE_SCRAP_CHARGING

    old_rows = read_csv(os.path.join(sm.data_dir, f"{heat_1}.csv"))
    tapping_ev = [e for e in old_rows if e["event"] == "tapping"][0]
    expected_end = t(45) + TAPPING_END_EXTENSION
    assert tapping_ev["end_time"] == expected_end.isoformat()
    print("test_tapping_15_minute_timeout_cap passed")


def test_next_scrap_closes_tapping():
    """Next scrap detection completes previous tapping and starts new heat."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    heat_1_id = sm.heat_id
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    sm._enter_tapping(tapping_start_time=t(30), sponge_end_time=t(30), note="test")
    assert sm.state == sm.STATE_TAPPING

    # Next scrap appears at t(40)
    keep_alive(sm, 31, 40)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(40))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id != heat_1_id
    print("test_next_scrap_closes_tapping passed")


def test_startup_midprocess_recovery():
    """Startup in IDLE recovers running heat if sponge or movement class appears before scrap."""
    for cls_id in (CLS_SPONGE_FEEDER_ENGAGED, CLS_SPONGE_FEEDER_ON_FURNACE,
                   CLS_FURNACE_POSITION_UP, CLS_FURNACE_TILTING, CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY):
        sm = fresh_machine()
        assert sm.state == sm.STATE_IDLE
        sm.process_detections({cls_id}, t(10))
        assert sm.state == sm.STATE_SPONGE_CHARGING
        assert sm.sponge_start == t(10)
    print("test_startup_midprocess_recovery passed")


def test_activefire_watchdog():
    """Heat is deleted if active fire is missing past timeout."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    heat_csv = sm.heat_csv_path
    assert os.path.exists(heat_csv)
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # Timeout passes without active fire
    timeout_mins = config.ACTIVEFIRE_TIMEOUT_MINUTES + 0.5
    sm.check_timeouts(t(timeout_mins))
    assert sm.state == sm.STATE_IDLE
    assert not os.path.exists(heat_csv)
    print("test_activefire_watchdog passed")


def test_status_and_overlay_output():
    """Test get_status keys and get_status_lines overlay text formatting."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    hb(sm, 23)

    # Movement away from normal
    sm.process_detections({CLS_FURNACE_TILTING}, t(25))
    st = sm.get_status()
    assert st["furnace_watch_active"] is True
    assert st["furnace_moving"] is True
    assert st["furnace_move_start"] == t(25).isoformat()
    assert st["tapping_candidate_active"] is False

    lines = sm.get_status_lines()
    assert any("Furnace: Moving" in line for line in lines)

    # Class 24 opens tapping check
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t(26))
    st2 = sm.get_status()
    assert st2["tapping_candidate_active"] is True
    assert st2["tapping_candidate_start"] == t(26).isoformat()
    assert st2["tapping_candidate_deadline"] == (t(26) + datetime.timedelta(minutes=5)).isoformat()

    lines2 = sm.get_status_lines()
    assert any("Furnace: bottom visible (tapping check running)" in line for line in lines2)
    print("test_status_and_overlay_output passed")


def test_class_36_starts_tapping_immediately():
    """Class 36 (laddle_placed_fortapping) is a first-class tapping trigger:
    tapping starts at that exact moment (no pre-roll, no window),
    and ends via the normal rules (here, class 1)."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    keep_alive(sm, 21, 29)

    # Class 36 at 10:30:00 -> tapping starts immediately.
    sm.process_detections({CLS_LADDLE_PLACED_FORTAPPING}, t(30))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(30)
    assert not sm.tap_candidate_active, "class 36 must not open a candidate window"
    assert "laddle_placed_fortapping (36)" in sm.last_event_desc

    # No PYRO change -> tapping does NOT complete on its own.
    temp_mon.set_temperature(1560.0)
    sm.check_timeouts(t(35))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(30)

    # Class 1 before any furnace movement must NOT end tapping
    sm.process_detections({CLS_FURNACE_NORMAL}, t(36))
    assert sm.state == sm.STATE_TAPPING
    assert "waiting for furnace movement" in sm.last_event_desc

    # Furnace movement at 10:40:00 (tilting)
    keep_alive(sm, 37, 39)
    sm.process_detections({CLS_FURNACE_TILTING}, t(40))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_movement_seen is True

    # Class 1 at 10:42:00 -> now tapping ends, heat completes.
    keep_alive(sm, 41, 41)
    sm.process_detections({CLS_FURNACE_NORMAL}, t(42))

    old_events = read_csv(os.path.join(sm.data_dir, "Heat_001_20260824_100000.csv"))
    taps = [e for e in old_events if e["event"] == "tapping"]
    sponge_evs = [e for e in old_events if e["event"] == "sponge_charging"]

    assert len(taps) == 1
    assert len(sponge_evs) == 1
    # Tapping starts at 36's timestamp - no 90-second backdate.
    assert taps[0]["start_time"] == t(30).isoformat()
    # Sponge charging is closed at the same moment tapping starts.
    assert sponge_evs[0]["end_time"] == t(30).isoformat()
    # Tapping end uses the same extension rule as the existing paths.
    assert taps[0]["end_time"] == (t(42) + TAPPING_END_EXTENSION).isoformat()
    print("test_class_36_starts_tapping_immediately passed")


def test_class_36_pre_empts_class_18_candidate_window():
    """If class 18 opened a window and class 36 lands during it, class 36
    wins: tapping starts at 36's timestamp, the window is discarded, no
    backdate is applied."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    keep_alive(sm, 21, 29)

    # Class 18 opens the candidate window at 10:30:00.
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t(30))
    assert sm.tap_candidate_active
    assert sm.tap_candidate_start == t(30)

    # Class 36 at 10:31:00 pre-empts the window.
    sm.process_detections({CLS_LADDLE_PLACED_FORTAPPING}, t(31))
    assert sm.state == sm.STATE_TAPPING
    assert not sm.tap_candidate_active
    assert sm.tapping_start == t(31)   # NOT t(30) - 90s = t(28.5)

    # Furnace movement then return to normal ends tapping
    keep_alive(sm, 32, 39)
    sm.process_detections({CLS_FURNACE_TILTING}, t(40))
    keep_alive(sm, 41, 44)
    sm.process_detections({CLS_FURNACE_NORMAL}, t(45))
    old_events = read_csv(os.path.join(sm.data_dir, "Heat_001_20260824_100000.csv"))
    taps = [e for e in old_events if e["event"] == "tapping"]
    assert len(taps) == 1
    assert taps[0]["start_time"] == t(31).isoformat()
    print("test_class_36_pre_empts_class_18_candidate_window passed")


def test_class_36_tapping_hits_15_minute_cap():
    """If class 1 never fires after class 36, the TAPPING_MAX_MINUTES cap
    completes the heat."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    keep_alive(sm, 21, 29)

    sm.process_detections({CLS_LADDLE_PLACED_FORTAPPING}, t(30))
    assert sm.state == sm.STATE_TAPPING

    keep_alive(sm, 31, 44)
    sm.check_timeouts(t(45))         # exactly 15 minutes after tapping_start
    assert sm.state != sm.STATE_TAPPING

    old_events = read_csv(os.path.join(sm.data_dir, "Heat_001_20260824_100000.csv"))
    taps = [e for e in old_events if e["event"] == "tapping"]
    assert taps[0]["start_time"] == t(30).isoformat()
    assert taps[0]["end_time"] == (t(45) + TAPPING_END_EXTENSION).isoformat()
    print("test_class_36_tapping_hits_15_minute_cap passed")


def test_class_18_path_unchanged():
    """Regression guard: class 18 + ladle 25 must still backdate tapping by
    TAPPING_PRE_ROLL and complete the heat immediately."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    keep_alive(sm, 21, 29)

    t18 = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t18)
    temp_mon.set_temperature(1565.0)
    sm.check_timeouts(t(31))
    # PYRO change must NOT confirm tapping
    assert sm.state == sm.STATE_SPONGE_CHARGING

    # Class 25 confirms tapping immediately
    sm.process_detections({CLS_LADDLE_MOVING_WITHMELTIRON}, t(31))

    # Heat completes synchronously on ladle confirmation
    assert sm.state == sm.STATE_IDLE
    old_events = read_csv(os.path.join(sm.data_dir, "Heat_001_20260824_100000.csv"))
    taps = [e for e in old_events if e["event"] == "tapping"]
    # Tapping start is 90 s before class 18's timestamp.
    assert taps[0]["start_time"] == (t18 - datetime.timedelta(seconds=90)).isoformat()
    print("test_class_18_path_unchanged passed")


def test_tapping_extension_and_auto_next_heat():
    """Tapping end is extended by TAPPING_END_EXTENSION, and a new heat
    starts in SCRAP_CHARGING at that extended time."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    heat_1 = sm.heat_id
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))
    sm._enter_tapping(tapping_start_time=t(30), sponge_end_time=t(30), note="test")

    keep_alive(sm, 21, 44)
    # Class 1 fires at t(45)
    sm.process_detections({CLS_FURNACE_NORMAL}, t(45))

    # A new heat is already running.
    assert sm.heat_id != heat_1
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # Old heat's tapping row ends at t(45) + extension.
    old_rows = read_csv(os.path.join(sm.data_dir, f"{heat_1}.csv"))
    taps = [e for e in old_rows if e["event"] == "tapping"]
    expected_end = t(45) + TAPPING_END_EXTENSION
    assert taps[0]["end_time"] == expected_end.isoformat()
    print("test_tapping_extension_and_auto_next_heat passed")


def test_camera_offline_suspends_activefire_watchdog():
    """Active fire watchdog is suspended while camera is offline, and heat survives reconnect."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    heat_id = sm.heat_id

    # Camera goes offline at t(5)
    sm.note_camera_state(False)
    assert not sm._camera_connected

    # Timeout passes while camera is down (26 minutes without class 5)
    sm.check_timeouts(t(26))
    # Heat MUST NOT be aborted!
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == heat_id
    assert os.path.exists(sm.heat_csv_path)

    # Camera reconnects at t(27)
    sm.note_camera_state(True)
    assert sm._camera_connected
    # First frame after reconnect does not trigger watchdog abort
    sm.check_timeouts(t(27))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == heat_id
    print("test_camera_offline_suspends_activefire_watchdog passed")


def test_provisional_heat_aborts_after_45min_without_scrap():
    sm = fresh_machine()
    sm._enter_tapping(tapping_start_time=t(30), sponge_end_time=t(30), note="test")
    keep_alive(sm, 31, 44)
    sm.process_detections({CLS_FURNACE_NORMAL}, t(45))    # heat completes, new one starts
    provisional = sm.heat_id
    csv_path = sm.heat_csv_path
    scrap_start = sm.scrap_start
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert os.path.exists(csv_path)

    # 44 minutes pass, still no scrap class
    keep_alive(sm, 47, 91)
    sm.check_timeouts(scrap_start + datetime.timedelta(minutes=44))
    assert sm.state == sm.STATE_SCRAP_CHARGING     # not yet

    # cross the 45-min mark
    sm.note_activefire_seen(scrap_start + datetime.timedelta(minutes=45))
    sm.check_timeouts(scrap_start + datetime.timedelta(minutes=45))
    assert sm.state == sm.STATE_IDLE
    assert sm.heat_id is None
    assert not os.path.exists(csv_path)
    print("test_provisional_heat_aborts_after_45min_without_scrap passed")


def test_provisional_heat_confirms_when_scrap_class_arrives():
    sm = fresh_machine()
    sm._enter_tapping(tapping_start_time=t(30), sponge_end_time=t(30), note="test")
    keep_alive(sm, 31, 44)
    sm.process_detections({CLS_FURNACE_NORMAL}, t(45))
    provisional = sm.heat_id
    scrap_start = sm.scrap_start
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # scrap class lands at T+10
    keep_alive(sm, 47, 54)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(55))
    assert sm._scrap_charging_confirmed is True

    # crossing 45-min mark now auto-transitions, doesn't abort
    keep_alive(sm, 56, 92)
    sm.check_timeouts(scrap_start + datetime.timedelta(minutes=45))
    assert sm.heat_id == provisional
    assert sm.state == sm.STATE_SPONGE_CHARGING
    print("test_provisional_heat_confirms_when_scrap_class_arrives passed")


def _seed_index(tmp_dir, rows, heat_events=None):
    """Write a heats_index.csv + one heat CSV per row."""
    os.makedirs(os.path.join(tmp_dir, "heats"), exist_ok=True)
    index_csv = os.path.join(tmp_dir, "heats_index.csv")
    with open(index_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["heat_id", "start_time", "end_time", "duration_minutes",
                    "tilt_count", "status", "csv_file"])
        for r in rows:
            w.writerow(r)
    events_map = heat_events or {}
    for r in rows:
        heat_id = r[0]
        csv_file = r[6] if len(r) > 6 and r[6] else f"{heat_id}.csv"
        p = os.path.join(tmp_dir, "heats", csv_file)
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["event", "start_time", "end_time",
                        "duration_minutes", "tilt_number", "note"])
            for e in events_map.get(heat_id, []):
                w.writerow(e)
    return index_csv


def test_resume_recent_incomplete_heat_from_scrap_charging():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    start = datetime.datetime.now() - datetime.timedelta(minutes=10)
    hid = f"Heat41_005_{start.strftime('%Y%m%d_%H%M%S')}"
    index_csv = _seed_index(TMP_DIR, [
        [hid, start.isoformat(), "", "", "0", "IN_PROGRESS", f"{hid}.csv"],
    ])

    sm = HeatStateMachine(
        data_dir=os.path.join(TMP_DIR, "heats"),
        index_csv=index_csv,
        temperature_monitor=MockTemperatureMonitor(1560.0),
    )

    assert sm.heat_id == hid
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_start == start
    # The next scrap class must NOT create a new heat.
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, start + datetime.timedelta(minutes=12))
    assert sm.heat_id == hid
    assert sm._scrap_charging_confirmed is True
    print("test_resume_recent_incomplete_heat_from_scrap_charging passed")


def test_resume_old_incomplete_heat_is_aborted():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    # Stale heat must be older than config.RESUME_INCOMPLETE_HEAT_MAX_MINUTES (now 120m)
    start = datetime.datetime.now() - datetime.timedelta(minutes=config.RESUME_INCOMPLETE_HEAT_MAX_MINUTES + 30)
    hid = f"Heat41_006_{start.strftime('%Y%m%d_%H%M%S')}"
    index_csv = _seed_index(TMP_DIR, [
        [hid, start.isoformat(), "", "", "0", "IN_PROGRESS", f"{hid}.csv"],
    ])

    sm = HeatStateMachine(
        data_dir=os.path.join(TMP_DIR, "heats"),
        index_csv=index_csv,
        temperature_monitor=MockTemperatureMonitor(1560.0),
    )

    assert sm.heat_id is None
    assert sm.state == sm.STATE_IDLE
    # Index row was retired, not left dangling.
    with open(index_csv) as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["status"] == "ABORTED"
    print("test_resume_old_incomplete_heat_is_aborted passed")


def test_resume_restores_sponge_charging_state_and_tilts():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    start = datetime.datetime.now() - datetime.timedelta(minutes=15)
    sponge_off = start + datetime.timedelta(minutes=5)
    hid = f"Heat41_007_{start.strftime('%Y%m%d_%H%M%S')}"
    tilt_end = start + datetime.timedelta(minutes=8)

    index_csv = _seed_index(
        TMP_DIR,
        [[hid, start.isoformat(), "", "", "1", "IN_PROGRESS", f"{hid}.csv"]],
        {hid: [
            ["scrap_charging", start.isoformat(), sponge_off.isoformat(),
             "5.0", "", ""],
            ["tilt", (tilt_end - datetime.timedelta(minutes=1)).isoformat(),
             tilt_end.isoformat(), "1.0", "1", ""],
        ]},
    )

    sm = HeatStateMachine(
        data_dir=os.path.join(TMP_DIR, "heats"),
        index_csv=index_csv,
        temperature_monitor=MockTemperatureMonitor(1560.0),
    )

    assert sm.heat_id == hid
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.scrap_start == start
    assert sm.sponge_start == sponge_off
    assert sm.tilt_count == 1
    assert sm.last_tilt_end == tilt_end
    assert sm._event_ordinal == 2   # two rows already on disk
    print("test_resume_restores_sponge_charging_state_and_tilts passed")


def test_resume_preserves_provisional_flag_for_tap_initiated_heat():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    prev_start = datetime.datetime.now() - datetime.timedelta(minutes=40)
    tap_end = prev_start + datetime.timedelta(minutes=35)   # COMPLETE
    # Auto-next heat starts at the exact tap-end timestamp.
    prov_start = tap_end
    prev_hid = f"Heat41_008_{prev_start.strftime('%Y%m%d_%H%M%S')}"
    prov_hid = f"Heat41_009_{prov_start.strftime('%Y%m%d_%H%M%S')}"

    index_csv = _seed_index(TMP_DIR, [
        [prev_hid, prev_start.isoformat(), tap_end.isoformat(),
         "35.0", "0", "COMPLETE", f"{prev_hid}.csv"],
        [prov_hid, prov_start.isoformat(), "", "", "0", "IN_PROGRESS",
         f"{prov_hid}.csv"],
    ])

    sm = HeatStateMachine(
        data_dir=os.path.join(TMP_DIR, "heats"),
        index_csv=index_csv,
        temperature_monitor=MockTemperatureMonitor(1560.0),
    )

    assert sm.heat_id == prov_hid
    assert sm.state == sm.STATE_SCRAP_CHARGING
    # Provisional flag preserved: the 45-min scrap-abort rule must still fire.
    assert sm._scrap_charging_confirmed is False

    # Cross the 45-min scrap threshold with no scrap class -> abort.
    keep_alive(sm, 1, 44, step=2)
    sm.note_activefire_seen(prov_start + datetime.timedelta(minutes=45))
    sm.check_timeouts(prov_start + datetime.timedelta(minutes=45, seconds=1))
    assert sm.state == sm.STATE_IDLE
    assert sm.heat_id is None
    print("test_resume_preserves_provisional_flag_for_tap_initiated_heat passed")


def test_index_upgrades_to_add_last_updated_column():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    index_csv = os.path.join(TMP_DIR, "heats_index.csv")
    # Old 7-column header, no last_updated
    with open(index_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["heat_id", "start_time", "end_time", "duration_minutes",
                    "tilt_count", "status", "csv_file"])
        w.writerow(["Heat_001_20260101_100000", "2026-01-01T10:00:00",
                    "", "", "0", "COMPLETE", "Heat_001_20260101_100000.csv"])

    HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                     index_csv=index_csv,
                     temperature_monitor=MockTemperatureMonitor())

    with open(index_csv, newline="") as f:
        rows = list(csv.DictReader(f))
    assert "last_updated" in rows[0]
    assert rows[0]["heat_id"] == "Heat_001_20260101_100000"
    print("test_index_upgrades_to_add_last_updated_column passed")


def test_heartbeat_updates_last_updated_while_running():
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    # Force the first heartbeat through the throttle.
    sm._last_index_touch_mono = 0.0
    sm._touch_index_row(force=True)
    with open(sm.index_csv, newline="") as f:
        row = next(csv.DictReader(f))
    assert row["last_updated"], "heartbeat must write a timestamp"
    print("test_heartbeat_updates_last_updated_while_running passed")


def test_resume_uses_last_updated_not_start_time():
    """A heat started 3 hours ago but alive 30 s ago must be RESUMED."""
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(os.path.join(TMP_DIR, "heats"), exist_ok=True)
    index_csv = os.path.join(TMP_DIR, "heats_index.csv")

    start = datetime.datetime.now() - datetime.timedelta(hours=3)
    touched = datetime.datetime.now() - datetime.timedelta(seconds=30)
    hid = f"Heat41_010_{start.strftime('%Y%m%d_%H%M%S')}"

    with open(index_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["heat_id", "start_time", "end_time", "duration_minutes",
                    "tilt_count", "status", "csv_file", "last_updated"])
        w.writerow([hid, start.isoformat(), "", "", "0", "IN_PROGRESS",
                    f"{hid}.csv", touched.isoformat()])
    with open(os.path.join(TMP_DIR, "heats", f"{hid}.csv"), "w", newline="") as f:
        csv.writer(f).writerow(["event", "start_time", "end_time",
                                "duration_minutes", "tilt_number", "note"])

    sm = HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                          index_csv=index_csv,
                          temperature_monitor=MockTemperatureMonitor())
    assert sm.heat_id == hid
    assert sm.state == sm.STATE_SCRAP_CHARGING
    print("test_resume_uses_last_updated_not_start_time passed")


def test_no_resume_when_heartbeat_is_stale():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(os.path.join(TMP_DIR, "heats"), exist_ok=True)
    index_csv = os.path.join(TMP_DIR, "heats_index.csv")

    start = datetime.datetime.now() - datetime.timedelta(minutes=config.RESUME_INCOMPLETE_HEAT_MAX_MINUTES + 60)
    touched = datetime.datetime.now() - datetime.timedelta(minutes=config.RESUME_INCOMPLETE_HEAT_MAX_MINUTES + 15)
    hid = f"Heat41_011_{start.strftime('%Y%m%d_%H%M%S')}"

    with open(index_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["heat_id", "start_time", "end_time", "duration_minutes",
                    "tilt_count", "status", "csv_file", "last_updated"])
        w.writerow([hid, start.isoformat(), "", "", "0", "IN_PROGRESS",
                    f"{hid}.csv", touched.isoformat()])
    with open(os.path.join(TMP_DIR, "heats", f"{hid}.csv"), "w", newline="") as f:
        csv.writer(f).writerow(["event", "start_time", "end_time",
                                "duration_minutes", "tilt_number", "note"])

    sm = HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                          index_csv=index_csv,
                          temperature_monitor=MockTemperatureMonitor())
    assert sm.heat_id is None
    assert sm.state == sm.STATE_IDLE
    with open(index_csv, newline="") as f:
        row = next(csv.DictReader(f))
    assert row["status"] == "ABORTED"
    print("test_no_resume_when_heartbeat_is_stale passed")


def test_maintenance_detection_classes():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    index_csv = os.path.join(TMP_DIR, "index.csv")

    sm = HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                          index_csv=index_csv,
                          temperature_monitor=MockTemperatureMonitor())

    classes_to_test = [
        CLS_COIL_POSITION_PLACEDNEARFURNACE,     # 0
        CLS_FURNACE_OPERATION_PREHEATING,        # 7
        CLS_FURNACE_OPERATION_UNDERMAINTENANCE,  # 28
        CLS_FURMA_POSITION_PLACEDNEARFURNACE,    # 39
    ]

    t = BASE
    for cid in classes_to_test:
        assert cid in UNDERMAINTENANCE_CLASS_IDS
        assert cid in sm.RELEVANT_CLASS_IDS
        sm.process_detections({cid}, t)
        assert "furnace under maintenance" in sm.last_event_desc
        pending = sm.get_pending_frame_requests()
        assert len(pending) == 1
        assert pending[0]["event_name"] == "furnace_undermaintenance"
        t += datetime.timedelta(seconds=1)

    print("test_maintenance_detection_classes passed")


def test_crash_recovery_with_active_state_json():
    """Verify that state.json persists in-flight heat data and crash recovery restores
    exact stage, tilt_count, and scrap_charging_confirmed."""
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    data_dir = os.path.join(TMP_DIR, "heats")
    index_csv = os.path.join(TMP_DIR, "heats_index.csv")

    sm1 = HeatStateMachine(data_dir=data_dir, index_csv=index_csv,
                           temperature_monitor=MockTemperatureMonitor())

    t0 = datetime.datetime.now() - datetime.timedelta(minutes=30)
    # Start heat with scrap
    sm1.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t0)
    heat_id = sm1.heat_id
    assert sm1.state == sm1.STATE_SCRAP_CHARGING
    assert sm1._scrap_charging_confirmed is True

    # State JSON must exist
    state_file = os.path.join(data_dir, f"{heat_id}.state.json")
    assert os.path.exists(state_file)

    # Transition to sponge charging
    t1 = t0 + datetime.timedelta(minutes=10)
    sm1.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t1)
    assert sm1.state == sm1.STATE_SPONGE_CHARGING

    # Perform a tilt
    t2 = t1 + datetime.timedelta(minutes=5)
    sm1.process_detections({CLS_FURNACE_TILTING}, t2)
    t3 = t2 + datetime.timedelta(seconds=20)
    sm1.process_detections({CLS_FURNACE_NORMAL}, t3)
    assert sm1.tilt_count == 1

    # Verify state.json content
    with open(state_file, "r") as f:
        saved = json.load(f)
    assert saved["state"] == "SPONGE_CHARGING"
    assert saved["tilt_count"] == 1
    assert saved["scrap_charging_confirmed"] is True

    # SIMULATE CORE DUMP & RESTART: destroy sm1 without finalize_tapping
    del sm1

    # Re-initialize state machine (as would happen on process supervisor restart)
    sm2 = HeatStateMachine(data_dir=data_dir, index_csv=index_csv,
                           temperature_monitor=MockTemperatureMonitor())

    # Heat must be seamlessly resumed!
    assert sm2.heat_id == heat_id
    assert sm2.state == sm2.STATE_SPONGE_CHARGING
    assert sm2.tilt_count == 1
    assert sm2._scrap_charging_confirmed is True
    # Camera watchdog suspended pending reconnection
    assert sm2._camera_connected is False

    # Simulate camera connects and detection arrives
    t4 = datetime.datetime.now()
    sm2.note_camera_state(True)
    assert sm2._camera_connected is True

    # Count another tilt on resumed heat
    t5 = t4 + datetime.timedelta(minutes=2)
    sm2.process_detections({CLS_FURNACE_TILTING}, t5)
    t6 = t5 + datetime.timedelta(seconds=20)
    sm2.process_detections({CLS_FURNACE_NORMAL}, t6)
    assert sm2.tilt_count == 2

    # Finish tapping
    t7 = t6 + datetime.timedelta(minutes=5)
    sm2.process_detections({CLS_LADDLE_PLACED_FORTAPPING}, t7)
    assert sm2.state == sm2.STATE_TAPPING

    t8 = t7 + datetime.timedelta(minutes=1)
    sm2.process_detections({CLS_FURNACE_TILTING}, t8)
    t9 = t8 + datetime.timedelta(seconds=30)
    sm2.process_detections({CLS_FURNACE_NORMAL}, t9)
    # Heat completed
    assert sm2.state == sm2.STATE_SCRAP_CHARGING  # auto_next heat started
    assert not os.path.exists(state_file)  # completed heat state file cleared

    print("test_crash_recovery_with_active_state_json passed")


if __name__ == "__main__":
    test_scrap_charging_class_ids()
    test_sponge_charging_class_ids()
    test_scrap_45min_automatic_timeout()
    test_tilt_counting_2_18_9()
    test_tilt_45_second_gap()
    test_bug_case_old_tilt_followed_by_tapping_much_later()
    test_expired_tapping_candidate()
    test_expired_tapping_candidate_without_class_9()
    test_pyro_change_does_not_confirm_tapping()
    test_activefire_loss_callback()
    test_other_triggers_class_25_ladle()
    test_edge_cases_repeat_24_and_class_25_alone()
    test_tapping_15_minute_timeout_cap()
    test_next_scrap_closes_tapping()
    test_startup_midprocess_recovery()
    test_activefire_watchdog()
    test_status_and_overlay_output()
    test_class_36_starts_tapping_immediately()
    test_class_36_pre_empts_class_18_candidate_window()
    test_class_36_tapping_hits_15_minute_cap()
    test_class_18_path_unchanged()
    test_tapping_extension_and_auto_next_heat()
    test_camera_offline_suspends_activefire_watchdog()
    test_provisional_heat_aborts_after_45min_without_scrap()
    test_provisional_heat_confirms_when_scrap_class_arrives()
    test_resume_recent_incomplete_heat_from_scrap_charging()
    test_resume_old_incomplete_heat_is_aborted()
    test_resume_restores_sponge_charging_state_and_tilts()
    test_resume_preserves_provisional_flag_for_tap_initiated_heat()
    test_index_upgrades_to_add_last_updated_column()
    test_heartbeat_updates_last_updated_while_running()
    test_resume_uses_last_updated_not_start_time()
    test_no_resume_when_heartbeat_is_stale()
    test_maintenance_detection_classes()
    test_crash_recovery_with_active_state_json()

    print("\nAll state-machine tests passed successfully!")
