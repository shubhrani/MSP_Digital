"""
Exercises the full heat cycle logic:

  1. Tilts under 130 min of sponge charging (furnace_position_tilding ->
     furnace_position_normal triggers count).
  2. The 130-minutes-of-sponge-charging "fast track" into tapping
     (furnace_position_tilding starts tapping, furnace_position_normal ends
     tapping).
  3. magnet_feeding_scrap starting a new heat.
  4. magnet_moving_withscrap positioned just above furnace_position_normal
     (the derived CLS_MAGNET_ABOVE_FURNACE signal) also starting a new heat.
  5. spange_feeder_onfurnace OR spange_feeder_engaged ending scrap charging
     and starting sponge charging.
  6. Tapping starts ONLY two ways: furnace_position_tilding held open past
     TILT_TO_TAPPING_MINUTES (3.5 min) with no furnace_position_normal
     ever arriving, or furnace_position_tilding detected once sponge
     charging has run for SPONGE_FAST_TRACK_MINUTES (130 min) or more.
  7. Hard time caps: scrap charging forced into sponge charging at
     SCRAP_CHARGING_MAX_MINUTES (40 min); sponge charging forced into
     tapping at SPONGE_CHARGING_MAX_MINUTES (140 min); tapping forced to
     end at TAPPING_MAX_MINUTES (7 min) with no furnace_position_normal -
     all three even with no qualifying detection at all.
  8. A heat with no confirmed furnace_operation_activefire for
     ACTIVEFIRE_TIMEOUT_MINUTES gets its data deleted (CSV + index row) and
     the state machine drops back to IDLE.

Detections are fed in by class NAME (matching how the real model/camera
worker feeds the state machine - see state_machine.CLS_* constants), not by
the model's numeric class ID, since the state machine matches on names.
CLS_MAGNET_ABOVE_FURNACE is not a class the model predicts - camera_worker.py
derives it from the raw magnet_moving_withscrap / furnace_position_normal
boxes each frame (see detector.magnet_above_furnace_normal()) - so it's fed
in here directly by name, the same way camera_worker.py would.

Run with:

    python3 tests/test_state_machine.py
"""

import csv
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_machine import HeatStateMachine, CLS_MAGNET_ABOVE_FURNACE  # noqa: E402

TMP_DIR = os.path.join(os.path.dirname(__file__), "_tmp_data")
BASE = datetime.datetime(2026, 8, 11, 8, 0, 0)

# Class names, matching state_machine.CLS_* (see the new model's class map
# in config.py). Kept as short local aliases just to keep the test bodies
# below readable.
MAGNET_FEEDING_SCRAP = "magnet_feeding_scrap"
MAGNET_ABOVE_FURNACE = CLS_MAGNET_ABOVE_FURNACE
SPONGE_FEEDER_ON_FURNACE = "spange_feeder_onfurnace"
SPONGE_FEEDER_ENGAGED = "spange_feeder_engaged"
FURNACE_TILTING = "furnace_position_tilding"
FURNACE_NORMAL = "furnace_position_normal"
FURNACE_ACTIVEFIRE = "furnace_operation_activefire"


def t(minutes_offset, base=BASE):
    return base + datetime.timedelta(minutes=minutes_offset)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def fresh_machine(**kwargs):
    if os.path.exists(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    return HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                             index_csv=os.path.join(TMP_DIR, "heats_index.csv"),
                             **kwargs)


def hb(sm, minutes_offset):
    """Simulates the camera worker's continuous per-frame heartbeat for
    furnace_operation_activefire - see
    CameraWorker.run()/state_machine.HeatStateMachine.note_activefire_seen.
    Keeps the active-fire watchdog's clock alive without touching any
    other part of the state machine."""
    sm.note_activefire_seen(t(minutes_offset))


def keep_alive(sm, start, end, step=8):
    """Calls hb() every `step` minutes across [start, end) - long enough
    that the active-fire watchdog (50 min default) never trips during a
    stretch of the test that isn't specifically exercising it."""
    m = start
    while m < end:
        hb(sm, m)
        m += step


# ---------------------------------------------------------------------------
def test_tilts_and_normal_tapping():
    """Heat 1: some tilts under 130 minutes, then over 130 min starts tapping."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))          # scrap charging starts
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == "Heat_001_20260811_080000"

    keep_alive(sm, 8, 25)
    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(25))     # scrap ends, sponge starts
    assert sm.state == sm.STATE_SPONGE_CHARGING

    keep_alive(sm, 33, 70)

    # First tilt: tilding -> normal
    sm.process_detections({FURNACE_TILTING}, t(70))
    assert sm.furnace_watch_active is True
    sm.process_detections({FURNACE_NORMAL}, t(71))
    assert sm.furnace_watch_active is False
    assert sm.tilt_count == 1

    # Second tilt (no cooldown anymore, immediately counts again if tilding->normal)
    sm.process_detections({FURNACE_TILTING}, t(72))
    sm.process_detections({FURNACE_NORMAL}, t(72.5))
    assert sm.tilt_count == 2

    keep_alive(sm, 80, 160)

    # Trigger tapping since we are over 130 mins (sponge started at 25, so >155)
    sm.process_detections({FURNACE_TILTING}, t(160))
    assert sm.state == sm.STATE_TAPPING

    keep_alive(sm, 168, 170)
    # End tapping with furnace_position_normal
    sm.process_detections({FURNACE_NORMAL}, t(170))
    assert sm.state == sm.STATE_IDLE

    # Heat 2 starts cleanly via magnet_feeding_scrap
    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(200))
    assert sm.heat_id == "Heat_002_20260811_112000"
    print("test_tilts_and_normal_tapping passed.")


# ---------------------------------------------------------------------------
def test_magnet_above_furnace_starts_scrap_charging():
    """magnet_moving_withscrap positioned just above furnace_position_normal
    (the derived CLS_MAGNET_ABOVE_FURNACE signal) starts a new heat exactly
    like magnet_feeding_scrap does."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_ABOVE_FURNACE}, t(0))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == "Heat_001_20260811_080000"
    assert sm.scrap_start == t(0)

    print("test_magnet_above_furnace_starts_scrap_charging passed.")


# ---------------------------------------------------------------------------
def test_sponge_charging_alt_trigger():
    """spange_feeder_engaged ends scrap charging and starts sponge charging,
    exactly like spange_feeder_onfurnace does."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 15)
    sm.process_detections({SPONGE_FEEDER_ENGAGED}, t(15))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.sponge_start == t(15)

    rows = read_csv(sm.get_heat_csv_path(sm.heat_id))
    scrap_row = next(r for r in rows if r["event"] == "scrap_charging")
    assert scrap_row["end_time"] == t(15).isoformat()

    print("test_sponge_charging_alt_trigger passed.")


# ---------------------------------------------------------------------------
def test_tilting_starts_tapping_after_3_5_minutes():
    """furnace_position_tilding held open for more than
    TILT_TO_TAPPING_MINUTES (3.5 min), under the 130-min fast track,
    without furnace_position_normal resolving it, is promoted straight into
    tapping - this and the 130-min fast track below are the ONLY two ways
    tapping starts."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(20))     # sponge starts, well under 130 min
    keep_alive(sm, 25, 30)

    sm.process_detections({FURNACE_TILTING}, t(30))              # tilt begins
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.furnace_watch_active is True

    # Under the threshold: still just a watched tilt.
    sm.check_timeouts(t(30) + datetime.timedelta(minutes=2.5))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.furnace_watch_active is True

    # Past the threshold: promoted to tapping, backdated to when the tilt
    # itself began (not to the moment the threshold was crossed).
    sm.check_timeouts(t(30) + datetime.timedelta(minutes=4.0))
    assert sm.state == sm.STATE_TAPPING
    assert sm.furnace_watch_active is False
    assert sm.tapping_start == t(30)

    sponge_row = next(r for r in read_csv(sm.get_heat_csv_path(sm.heat_id))
                      if r["event"] == "sponge_charging")
    assert sponge_row["end_time"] == t(30).isoformat()
    assert "3.5" in sponge_row["note"]

    print("test_tilting_starts_tapping_after_3_5_minutes passed.")


# ---------------------------------------------------------------------------
def test_fast_track_after_130_min_and_tapping_end():
    """A heat where sponge charging runs past 130 minutes: tilding starts
    tapping, and furnace_position_normal ends tapping."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 10)
    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(10))     # sponge_start = t(10)
    assert sm.state == sm.STATE_SPONGE_CHARGING

    keep_alive(sm, 15, 145)

    # 135 minutes into sponge charging (>= 130) - tilding starts tapping
    detect_time = t(10 + 135)
    sm.process_detections({FURNACE_TILTING}, detect_time)

    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == detect_time
    assert sm.furnace_watch_active is False

    heat_csv_path = sm.get_heat_csv_path(sm.heat_id)
    rows = read_csv(heat_csv_path)
    sponge_row = next(r for r in rows if r["event"] == "sponge_charging")
    assert sponge_row["end_time"] == detect_time.isoformat()
    expected_sponge_minutes = round((detect_time - t(10)).total_seconds() / 60.0, 2)
    assert float(sponge_row["duration_minutes"]) == expected_sponge_minutes

    keep_alive(sm, 150, 160)

    # tapping ends when furnace_position_normal is detected
    end_time = detect_time + datetime.timedelta(minutes=15)
    old_heat_id = sm.heat_id
    sm.process_detections({FURNACE_NORMAL}, end_time)

    assert sm.state == sm.STATE_IDLE
    assert sm.heat_id == old_heat_id

    old_rows = read_csv(sm.get_heat_csv_path(old_heat_id))
    tapping_row = next(r for r in old_rows if r["event"] == "tapping")
    assert tapping_row["end_time"] == end_time.isoformat()
    expected_tap_minutes = round((end_time - detect_time).total_seconds() / 60.0, 2)
    assert float(tapping_row["duration_minutes"]) == expected_tap_minutes

    index_rows = {r["heat_id"]: r for r in sm.get_heats_history()}
    assert index_rows[old_heat_id]["status"] == "COMPLETE"

    print("test_fast_track_after_130_min_and_tapping_end passed.")


# ---------------------------------------------------------------------------
def test_mid_process_start_into_sponge_charging():
    """App is (re)started while the furnace is already mid-sponge-charging:
    the very first detection ever seen is spange_feeder_onfurnace, not
    magnet_feeding_scrap. The heat should be picked up immediately in
    SPONGE_CHARGING rather than sitting in IDLE waiting for a
    scrap-charging detection that already happened."""
    sm = fresh_machine()

    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(0))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.sponge_start == t(0)
    assert sm.scrap_start is None

    rows = read_csv(sm.get_heat_csv_path(sm.heat_id))
    assert rows[0]["event"] == "scrap_charging_unknown"
    assert rows[0]["start_time"] == ""
    assert rows[0]["end_time"] == ""

    keep_alive(sm, 8, 140)

    # Tapping sequence triggers normally after 130 min
    sm.process_detections({FURNACE_TILTING}, t(140))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(140)

    keep_alive(sm, 145, 155)

    # Ends via furnace_position_normal
    sm.process_detections({FURNACE_NORMAL}, t(155))
    assert sm.state == sm.STATE_IDLE

    print("test_mid_process_start_into_sponge_charging passed.")


# ---------------------------------------------------------------------------
def test_mid_process_start_into_tapping():
    """App is (re)started while the furnace is already mid-tapping: the
    very first detection ever seen is furnace_position_tilding, held past
    TILT_TO_TAPPING_MINUTES. Since no class definitively means "already
    tapping" on its own anymore, the pickup starts in SPONGE_CHARGING and
    the normal tilt-promotion rule takes it into TAPPING from there, with
    scrap/sponge both logged as unknown-duration."""
    sm = fresh_machine()

    sm.process_detections({FURNACE_TILTING}, t(0))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.scrap_start is None
    assert sm.sponge_start == t(0)
    assert sm.furnace_watch_active is True

    rows = read_csv(sm.get_heat_csv_path(sm.heat_id))
    assert rows[0]["event"] == "scrap_charging_unknown"

    sm.check_timeouts(t(0) + datetime.timedelta(minutes=4.0))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(0)

    sm.process_detections({FURNACE_NORMAL}, t(10))
    assert sm.state == sm.STATE_IDLE

    print("test_mid_process_start_into_tapping passed.")


# ---------------------------------------------------------------------------
def test_tapping_hard_cap():
    """Tapping cannot run longer than TAPPING_MAX_MINUTES (7 min) - if
    furnace_position_normal hasn't been detected by then, the state machine
    forces tapping to end (and the heat to complete) anyway, purely from
    check_timeouts()."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(20))
    keep_alive(sm, 25, 155)

    # Fast-track into tapping (well past 130 min of sponge charging)
    tap_start = t(20 + 135)
    sm.process_detections({FURNACE_TILTING}, tap_start)
    assert sm.state == sm.STATE_TAPPING
    heat_id = sm.heat_id

    # Just under the cap: still tapping.
    sm.check_timeouts(tap_start + datetime.timedelta(minutes=6.5))
    assert sm.state == sm.STATE_TAPPING
    assert sm.heat_id == heat_id

    # Past the cap: forced to end, heat completes, back to IDLE.
    cap_time = tap_start + datetime.timedelta(minutes=7.5)
    sm.check_timeouts(cap_time)
    assert sm.state == sm.STATE_IDLE

    old_rows = read_csv(sm.get_heat_csv_path(heat_id))
    tapping_row = next(r for r in old_rows if r["event"] == "tapping")
    assert tapping_row["end_time"] == cap_time.isoformat()
    assert "7" in tapping_row["note"]

    index_rows = {r["heat_id"]: r for r in sm.get_heats_history()}
    assert index_rows[heat_id]["status"] == "COMPLETE"

    print("test_tapping_hard_cap passed.")


# ---------------------------------------------------------------------------
def test_scrap_charging_hard_cap():
    """Scrap charging cannot run longer than SCRAP_CHARGING_MAX_MINUTES
    (40 min) - if no sponge_charging_start signal has arrived by then, the
    state machine forces the transition into sponge charging anyway, purely
    from check_timeouts()."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # Just under the cap: still scrap charging.
    sm.check_timeouts(t(39))
    assert sm.state == sm.STATE_SCRAP_CHARGING

    # Past the cap: forced into sponge charging.
    sm.check_timeouts(t(40.5))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.sponge_start == t(40.5)

    scrap_row = next(r for r in read_csv(sm.get_heat_csv_path(sm.heat_id))
                     if r["event"] == "scrap_charging")
    assert scrap_row["end_time"] == t(40.5).isoformat()
    assert "40" in scrap_row["note"]

    print("test_scrap_charging_hard_cap passed.")


# ---------------------------------------------------------------------------
def test_sponge_charging_hard_cap():
    """Sponge charging cannot run longer than SPONGE_CHARGING_MAX_MINUTES
    (140 min) - if tapping hasn't started by then (e.g. no
    furnace_position_tilding sighting ever arrived to trigger the 130-min
    fast track), the state machine forces tapping to start anyway."""
    sm = fresh_machine()

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    keep_alive(sm, 5, 10)
    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(10))     # sponge_start = t(10)
    keep_alive(sm, 15, 145)

    # Just under the cap (140 min of sponge charging = t(150)): still sponge charging.
    sm.check_timeouts(t(10 + 139))
    assert sm.state == sm.STATE_SPONGE_CHARGING

    # Past the cap: forced into tapping.
    cap_time = t(10 + 140.5)
    sm.check_timeouts(cap_time)
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == cap_time

    sponge_row = next(r for r in read_csv(sm.get_heat_csv_path(sm.heat_id))
                      if r["event"] == "sponge_charging")
    assert sponge_row["end_time"] == cap_time.isoformat()
    assert "140" in sponge_row["note"]

    print("test_sponge_charging_hard_cap passed.")


# ---------------------------------------------------------------------------
def test_heat_deleted_after_activefire_timeout():
    """If furnace_operation_activefire is never (re)confirmed for
    ACTIVEFIRE_TIMEOUT_MINUTES while a heat is running, the heat's CSV file
    and its row in the index CSV are both deleted and the state machine
    drops back to IDLE. Moved into sponge charging (140-min cap) right
    away so the 40-min scrap-charging cap doesn't interfere with the
    50-min activefire window being tested here."""
    sm = fresh_machine(furnace_id="4/2")

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(1))
    heat_id = sm.heat_id
    csv_path = sm.get_heat_csv_path(heat_id)
    assert csv_path is not None and os.path.exists(csv_path)
    assert heat_id in {r["heat_id"] for r in sm.get_heats_history()}

    # No furnace_operation_activefire at all. Just under the timeout, the
    # heat is still there.
    sm.check_timeouts(t(49))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.heat_id == heat_id

    # Past the timeout: the heat is wiped - CSV file, index row, and all.
    sm.check_timeouts(t(50.5))
    assert sm.state == sm.STATE_IDLE
    assert sm.heat_id is None
    assert not os.path.exists(csv_path)
    assert heat_id not in {r["heat_id"] for r in sm.get_heats_history()}

    # A fresh detection starts a brand new heat, unaffected by the deleted one.
    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(60))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id != heat_id

    print("test_heat_deleted_after_activefire_timeout passed.")


def test_activefire_seen_partway_through_resets_the_clock():
    """A late but in-time furnace_operation_activefire sighting resets the
    watchdog clock, so a heat that would otherwise be right on the edge
    survives. Moved into sponge charging (140-min cap) right away so the
    40-min scrap-charging cap doesn't interfere with the 100-min window
    being tested here."""
    sm = fresh_machine(furnace_id="4/2")

    sm.process_detections({MAGNET_FEEDING_SCRAP}, t(0))
    sm.process_detections({SPONGE_FEEDER_ON_FURNACE}, t(1))
    heat_id = sm.heat_id

    sm.check_timeouts(t(49))                     # still fine, under 50 min
    assert sm.heat_id == heat_id

    sm.process_detections({FURNACE_ACTIVEFIRE}, t(49.5))    # clock resets
    sm.check_timeouts(t(98))                     # 48.5 min after the sighting: fine
    assert sm.heat_id == heat_id
    assert sm.state == sm.STATE_SPONGE_CHARGING

    sm.check_timeouts(t(100))                     # 50.5 min after the sighting: wiped
    assert sm.state == sm.STATE_IDLE
    assert sm.heat_id is None

    print("test_activefire_seen_partway_through_resets_the_clock passed.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_tilts_and_normal_tapping()
    test_magnet_above_furnace_starts_scrap_charging()
    test_sponge_charging_alt_trigger()
    test_tilting_starts_tapping_after_3_5_minutes()
    test_fast_track_after_130_min_and_tapping_end()
    test_mid_process_start_into_sponge_charging()
    test_mid_process_start_into_tapping()
    test_tapping_hard_cap()
    test_scrap_charging_hard_cap()
    test_sponge_charging_hard_cap()
    test_heat_deleted_after_activefire_timeout()
    test_activefire_seen_partway_through_resets_the_clock()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("All state machine tests passed.")
