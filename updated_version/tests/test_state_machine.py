"""
Exercises the full heat cycle logic:

  1. Tilts under 130 min of sponge charging (12 -> 2 triggers count).
  2. The 130-minutes-of-sponge-charging "fast track" into tapping (12 starts tapping, 2 ends tapping).
  3. magnet_feeding_scrap (class 3) starting a new heat.
  4. A tilt held open past TILT_TO_TAPPING_MINUTES gets promoted to tapping
     even with no class 2 ever arriving.
  5. A heat with no confirmed class 9 (furnace_operation_activefire) for
     ACTIVEFIRE_TIMEOUT_MINUTES gets its data deleted (CSV + index row) and
     the state machine drops back to IDLE.

Run with:

    python3 tests/test_state_machine.py
"""

import csv
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_machine import HeatStateMachine  # noqa: E402

TMP_DIR = os.path.join(os.path.dirname(__file__), "_tmp_data")
BASE = datetime.datetime(2026, 8, 11, 8, 0, 0)


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
    class 9 (furnace_operation_activefire) - see
    CameraWorker.run()/state_machine.HeatStateMachine.note_activefire_seen.
    Keeps the active-fire watchdog's clock alive without touching any
    other part of the state machine."""
    sm.note_activefire_seen(t(minutes_offset))


def keep_alive(sm, start, end, step=8):
    """Calls hb() every `step` minutes across [start, end) - long enough
    that the active-fire watchdog (10 min default) never trips during a
    stretch of the test that isn't specifically exercising it."""
    m = start
    while m < end:
        hb(sm, m)
        m += step


# ---------------------------------------------------------------------------
def test_tilts_and_normal_tapping():
    """Heat 1: some tilts under 130 minutes, then over 130 min starts tapping."""
    sm = fresh_machine()

    sm.process_detections({3}, t(0))          # scrap charging starts
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == "Heat_001_20260811_080000"

    keep_alive(sm, 8, 25)
    sm.process_detections({8}, t(25))         # scrap ends, sponge starts
    assert sm.state == sm.STATE_SPONGE_CHARGING

    keep_alive(sm, 33, 70)

    # First tilt: 12 -> 2
    sm.process_detections({12}, t(70))
    assert sm.furnace_watch_active is True
    sm.process_detections({2}, t(71))
    assert sm.furnace_watch_active is False
    assert sm.tilt_count == 1

    # Second tilt (no cooldown anymore, immediately counts again if 12->2)
    sm.process_detections({12}, t(72))
    sm.process_detections({2}, t(72.5))
    assert sm.tilt_count == 2

    keep_alive(sm, 80, 160)

    # Trigger tapping since we are over 130 mins (sponge started at 25, so >155)
    sm.process_detections({12}, t(160))
    assert sm.state == sm.STATE_TAPPING

    keep_alive(sm, 168, 170)
    # End tapping with 2
    sm.process_detections({2}, t(170))
    assert sm.state == sm.STATE_IDLE

    # Heat 2 starts cleanly via magnet
    sm.process_detections({3}, t(200))
    assert sm.heat_id == "Heat_002_20260811_112000"
    print("test_tilts_and_normal_tapping passed.")


# ---------------------------------------------------------------------------
def test_fast_track_after_130_min_and_tapping_end():
    """A heat where sponge charging runs past 130 minutes: 12 starts tapping, 
    and 2 ends tapping."""
    sm = fresh_machine()

    sm.process_detections({3}, t(0))
    keep_alive(sm, 5, 10)
    sm.process_detections({8}, t(10))          # sponge_start = t(10)
    assert sm.state == sm.STATE_SPONGE_CHARGING

    keep_alive(sm, 15, 145)

    # 135 minutes into sponge charging (>= 130) - class 12 starts tapping
    detect_time = t(10 + 135)
    sm.process_detections({12}, detect_time)

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

    # tapping ends when 2 is detected
    end_time = detect_time + datetime.timedelta(minutes=15)
    old_heat_id = sm.heat_id
    sm.process_detections({2}, end_time)

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
    the very first detection ever seen is class 8, not class 3. The heat
    should be picked up immediately in SPONGE_CHARGING rather than sitting
    in IDLE waiting for a scrap-charging detection that already happened."""
    sm = fresh_machine()

    sm.process_detections({8}, t(0))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.sponge_start == t(0)
    assert sm.scrap_start is None

    rows = read_csv(sm.get_heat_csv_path(sm.heat_id))
    assert rows[0]["event"] == "scrap_charging_unknown"
    assert rows[0]["start_time"] == ""
    assert rows[0]["end_time"] == ""

    keep_alive(sm, 8, 140)

    # Tapping sequence triggers normally after 130 min
    sm.process_detections({12}, t(140))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(140)

    keep_alive(sm, 145, 155)

    # Ends via class 2
    sm.process_detections({2}, t(155))
    assert sm.state == sm.STATE_IDLE

    print("test_mid_process_start_into_sponge_charging passed.")


# ---------------------------------------------------------------------------
def test_tilt_over_threshold_promoted_to_tapping():
    """A tilt that never resolves via class 2 (and hasn't reached the
    130-minute fast track) is promoted straight into tapping once it has
    been open for more than TILT_TO_TAPPING_MINUTES (1.9 min) - a pure
    time-based check, so it fires purely from check_timeouts() without any
    further detection ever arriving for that movement."""
    sm = fresh_machine()

    sm.process_detections({3}, t(0))
    keep_alive(sm, 5, 20)
    sm.process_detections({8}, t(20))           # sponge starts, well under 130 min
    keep_alive(sm, 25, 30)

    sm.process_detections({12}, t(30))           # tilt begins
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.furnace_watch_active is True

    # Under the threshold: still just a watched tilt.
    sm.check_timeouts(t(30) + datetime.timedelta(minutes=1.0))
    assert sm.state == sm.STATE_SPONGE_CHARGING
    assert sm.furnace_watch_active is True

    # Past the threshold: promoted to tapping, backdated to when the tilt
    # itself began (not to the moment the threshold was crossed).
    sm.check_timeouts(t(30) + datetime.timedelta(minutes=2.0))
    assert sm.state == sm.STATE_TAPPING
    assert sm.furnace_watch_active is False
    assert sm.tapping_start == t(30)

    sponge_row = next(r for r in read_csv(sm.get_heat_csv_path(sm.heat_id))
                      if r["event"] == "sponge_charging")
    assert sponge_row["end_time"] == t(30).isoformat()
    assert "1.9" in sponge_row["note"]

    print("test_tilt_over_threshold_promoted_to_tapping passed.")


# ---------------------------------------------------------------------------
def test_heat_deleted_after_activefire_timeout():
    """If class 9 is never (re)confirmed for ACTIVEFIRE_TIMEOUT_MINUTES
    while a heat is running, the heat's CSV file and its row in the index
    CSV are both deleted and the state machine drops back to IDLE."""
    sm = fresh_machine(furnace_id="4/2")

    sm.process_detections({3}, t(0))
    heat_id = sm.heat_id
    csv_path = sm.get_heat_csv_path(heat_id)
    assert csv_path is not None and os.path.exists(csv_path)
    assert heat_id in {r["heat_id"] for r in sm.get_heats_history()}

    # No class 9 at all. Just under the timeout, the heat is still there.
    sm.check_timeouts(t(49))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == heat_id

    # Past the timeout: the heat is wiped - CSV file, index row, and all.
    sm.check_timeouts(t(50.5))
    assert sm.state == sm.STATE_IDLE
    assert sm.heat_id is None
    assert not os.path.exists(csv_path)
    assert heat_id not in {r["heat_id"] for r in sm.get_heats_history()}

    # A fresh detection starts a brand new heat, unaffected by the deleted one.
    sm.process_detections({3}, t(60))
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id != heat_id

    print("test_heat_deleted_after_activefire_timeout passed.")


def test_activefire_seen_partway_through_resets_the_clock():
    """A late but in-time class 9 sighting resets the watchdog clock, so a
    heat that would otherwise be right on the edge survives."""
    sm = fresh_machine(furnace_id="4/2")

    sm.process_detections({3}, t(0))
    heat_id = sm.heat_id

    sm.check_timeouts(t(49))                     # still fine, under 50 min
    assert sm.heat_id == heat_id

    sm.process_detections({9}, t(49.5))           # activefire confirmed - clock resets
    sm.check_timeouts(t(98))                     # 48.5 min after the sighting: fine
    assert sm.heat_id == heat_id
    assert sm.state == sm.STATE_SCRAP_CHARGING

    sm.check_timeouts(t(100))                     # 50.5 min after the sighting: wiped
    assert sm.state == sm.STATE_IDLE
    assert sm.heat_id is None

    print("test_activefire_seen_partway_through_resets_the_clock passed.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_tilts_and_normal_tapping()
    test_fast_track_after_130_min_and_tapping_end()
    test_mid_process_start_into_sponge_charging()
    test_tilt_over_threshold_promoted_to_tapping()
    test_heat_deleted_after_activefire_timeout()
    test_activefire_seen_partway_through_resets_the_clock()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("All state machine tests passed.")
