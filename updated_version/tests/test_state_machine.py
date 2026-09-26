"""Regression tests for the plant heat sequence (38-class model + InfluxDB temperature).

Lifecycle:
    scrap (16, 17, 19) -> sponge (6, 10, 35, 37 or 45-min cap) -> tapping (class 24 + PYRO/ladle 25) -> complete

Key rules:
    - 16, 17, 19 start scrap charging.
    - 6, 10, 35, 37 transition scrap to sponge charging.
    - Scrap charging auto-transitions to sponge charging at scrap_start + 45 minutes if no sponge class is detected.
    - During sponge charging:
        * Movement cycle: 2, 8, 18, 24 start movement; class 9 ends movement.
        * Tilts count on return to normal (class 9) with a 45s minimum gap between tilts.
        * Tapping candidate: opened strictly by class 24 (5-minute window).
        * Ladle (25) during candidate confirms tapping immediately.
        * PYRO change on either furnace confirms tapping.
        * Tapping start time = class 24 time - 90s pre-roll (clamped to sponge start).
        * Tapping confirmation completes the heat immediately.
        * Expired candidate (5 min without PYRO/ladle) resolves pending movement as tilt.
    - Active-fire watchdog purges heats where class 3 has been missing for 10 minutes.
    - Mid-process startup recovery supports sponge classes and movement classes.
"""
import csv
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from state_machine import (
    HeatStateMachine,
    CLS_FURNACE_POSITION_UP,
    CLS_FURNACE_ACTIVEFIRE,
    CLS_SPONGE_FEEDER_ENGAGED,
    CLS_FURNACE_BOTTOM_VISIBLE_PARTIALLY,
    CLS_FURNACE_NORMAL,
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_MAGNET_MOVING_WITH_SCRAP,
    CLS_MAGNET_FEEDING_SCRAP,
    CLS_FURNACE_TILTING,
    CLS_FURNACE_OPERATION_SCRAPFEEDING,
    CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY,
    CLS_LADDLE_MOVING_WITHMELTIRON,
    CLS_MAGNET_MOVING_WITH_SPONGE,
    CLS_MAGNET_FEEDING_SPONGE,
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
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


def fresh_machine(temp_monitor=None, on_activefire_loss=None, on_maintenance_detected=None):
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    return HeatStateMachine(
        data_dir=os.path.join(TMP_DIR, "heats"),
        index_csv=os.path.join(TMP_DIR, "heats_index.csv"),
        temperature_monitor=temp_monitor or MockTemperatureMonitor(1560.0),
        on_activefire_loss=on_activefire_loss,
        on_maintenance_detected=on_maintenance_detected,
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
    """Verify classes 16, 17, and 19 all start scrap charging."""
    for cls_id in (CLS_MAGNET_MOVING_WITH_SCRAP, CLS_MAGNET_FEEDING_SCRAP, CLS_FURNACE_OPERATION_SCRAPFEEDING):
        sm = fresh_machine()
        assert sm.state == sm.STATE_IDLE
        sm.process_detections({cls_id}, t(0))
        assert sm.state == sm.STATE_SCRAP_CHARGING
        assert sm.scrap_start == t(0)
    print("test_scrap_charging_class_ids passed")


def test_sponge_charging_class_ids():
    """Verify classes 6, 10, 35, and 37 transition scrap to sponge charging."""
    for cls_id in (CLS_SPONGE_FEEDER_ENGAGED, CLS_SPONGE_FEEDER_ON_FURNACE,
                   CLS_MAGNET_MOVING_WITH_SPONGE, CLS_MAGNET_FEEDING_SPONGE):
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

    # At 12:01:00 (t(121)), PYRO changes 1560 -> 1572
    temp_mon.set_temperature(1572.0)
    t_pyro = t(121)
    sm.check_timeouts(t_pyro)

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
    assert tapping_evs[0]["end_time"] == t_pyro.isoformat()

    # Duration of tapping should be 2.5 minutes (150s), NOT 91 minutes!
    assert float(tapping_evs[0]["duration_minutes"]) == 2.5
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


def test_own_pyro_confirms_tapping():
    """A PYRO change on this furnace's own monitor confirms tapping."""
    own_temp = MockTemperatureMonitor(1560.0)
    own_temp.field = "PYRO4/1"
    sm = fresh_machine(own_temp)

    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    # Class 24 opens tapping check
    t24 = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t24)
    assert sm.tap_candidate_active

    # Own furnace PYRO changes at 10:31:00
    own_temp.set_temperature(1568.0)
    t_change = t(31)
    sm.check_timeouts(t_change)

    assert sm.state == sm.STATE_IDLE
    events = read_csv(sm.heat_csv_path)
    taps = [e for e in events if e["event"] == "tapping"]
    assert len(taps) == 1
    assert "PYRO4/1 changed 1560.0 -> 1568.0" in taps[0]["note"]
    print("test_own_pyro_confirms_tapping passed")


def test_activefire_loss_callback():
    """Active fire loss aborts the heat and fires the on_activefire_loss callback."""
    loss_called = []
    def on_loss():
        loss_called.append(True)

    sm = fresh_machine(on_activefire_loss=on_loss)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # 5.5 minutes pass without active fire (timeout is 5 min)
    sm.check_timeouts(t(5.5))
    assert sm.state == sm.STATE_IDLE
    assert len(loss_called) == 1
    print("test_activefire_loss_callback passed")


def test_maintenance_detected_callback():
    """Class 28 confirmed invokes on_maintenance_detected callback."""
    maint_called = []
    def on_maint():
        maint_called.append(True)

    sm = fresh_machine(on_maintenance_detected=on_maint)
    sm.process_detections({CLS_FURNACE_OPERATION_UNDERMAINTENANCE}, t(0))
    assert len(maint_called) == 1
    assert "furnace under maintenance" in sm.last_event_desc
    print("test_maintenance_detected_callback passed")


def test_other_triggers_class_25_ladle():
    """Class 25 after class 24 confirms tapping with no PYRO change."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    t24 = t(30)
    sm.process_detections({CLS_FURNACE_BOTTOM_VISIBLE_COMPLETELY}, t24)
    assert sm.tap_candidate_active

    # Class 25 (ladle moving with melt iron) confirmed at 10:31:00
    t25 = t(31)
    sm.process_detections({CLS_LADDLE_MOVING_WITHMELTIRON}, t25)

    assert sm.state == sm.STATE_IDLE
    events = read_csv(sm.heat_csv_path)
    taps = [e for e in events if e["event"] == "tapping"]
    assert len(taps) == 1
    assert "laddle_moving_withmeltiron (25)" in taps[0]["note"]
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
    """Tapping safely caps and completes at 15 minutes if class 9 is missed."""
    temp_mon = MockTemperatureMonitor(1560.0)
    sm = fresh_machine(temp_mon)
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t(20))

    keep_alive(sm, 21, 29)
    # Manually transition to tapping to test the 15-minute cap watchdog
    sm._enter_tapping(tapping_start_time=t(30), sponge_end_time=t(30), note="test")
    assert sm.state == sm.STATE_TAPPING

    keep_alive(sm, 31, 45)
    sm.check_timeouts(t(45))  # exactly 15 minutes
    assert sm.state == sm.STATE_IDLE

    events = read_csv(sm.heat_csv_path)
    tapping_ev = [e for e in events if e["event"] == "tapping"][0]
    assert float(tapping_ev["duration_minutes"]) == 15.0
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
    sm.process_detections({CLS_MAGNET_MOVING_WITH_SCRAP}, t(40))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id != heat_1_id
    print("test_next_scrap_closes_tapping passed")


def test_startup_midprocess_recovery():
    """Startup in IDLE recovers running heat if sponge or movement class appears before scrap."""
    for cls_id in (CLS_SPONGE_FEEDER_ENGAGED, CLS_MAGNET_MOVING_WITH_SPONGE,
                   CLS_FURNACE_POSITION_UP, CLS_FURNACE_TILTING, CLS_LADDLE_MOVING_WITHMELTIRON):
        sm = fresh_machine()
        assert sm.state == sm.STATE_IDLE
        sm.process_detections({cls_id}, t(10))
        assert sm.state == sm.STATE_SPONGE_CHARGING
        assert sm.sponge_start == t(10)
    print("test_startup_midprocess_recovery passed")


def test_activefire_watchdog():
    """Heat is deleted if class 3 is not seen for 5 minutes."""
    sm = fresh_machine()
    sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t(0))
    heat_csv = sm.heat_csv_path
    assert os.path.exists(heat_csv)
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # 5.5 minutes pass without active fire (timeout is 5 min)
    sm.check_timeouts(t(5.5))
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


if __name__ == "__main__":
    test_scrap_charging_class_ids()
    test_sponge_charging_class_ids()
    test_scrap_45min_automatic_timeout()
    test_tilt_counting_2_18_9()
    test_tilt_45_second_gap()
    test_bug_case_old_tilt_followed_by_tapping_much_later()
    test_expired_tapping_candidate()
    test_expired_tapping_candidate_without_class_9()
    test_own_pyro_confirms_tapping()
    test_activefire_loss_callback()
    test_maintenance_detected_callback()
    test_other_triggers_class_25_ladle()
    test_edge_cases_repeat_24_and_class_25_alone()
    test_tapping_15_minute_timeout_cap()
    test_next_scrap_closes_tapping()
    test_startup_midprocess_recovery()
    test_activefire_watchdog()
    test_status_and_overlay_output()

    print("\nAll state-machine tests passed successfully!")
