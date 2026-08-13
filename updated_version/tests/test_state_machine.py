"""
Exercises the full heat cycle logic:

  1. Tilts under 130 min of sponge charging (12 -> 2 triggers count).
  2. The 130-minutes-of-sponge-charging "fast track" into tapping (12 starts tapping, 2 ends tapping).
  3. magnet_feeding_scrap (class 3) starting a new heat.

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


def fresh_machine():
    if os.path.exists(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    return HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                             index_csv=os.path.join(TMP_DIR, "heats_index.csv"))


# ---------------------------------------------------------------------------
def test_tilts_and_normal_tapping():
    """Heat 1: some tilts under 130 minutes, then over 130 min starts tapping."""
    sm = fresh_machine()

    sm.process_detections({3}, t(0))          # scrap charging starts
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == "Heat_001_20260811_080000"

    sm.process_detections({8}, t(25))         # scrap ends, sponge starts
    assert sm.state == sm.STATE_SPONGE_CHARGING

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

    # Trigger tapping since we are over 130 mins (sponge started at 25, so >155)
    sm.process_detections({12}, t(160))
    assert sm.state == sm.STATE_TAPPING
    
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
    sm.process_detections({8}, t(10))          # sponge_start = t(10)
    assert sm.state == sm.STATE_SPONGE_CHARGING

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

    # Tapping sequence triggers normally after 130 min
    sm.process_detections({12}, t(140))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(140)

    # Ends via class 2
    sm.process_detections({2}, t(155))
    assert sm.state == sm.STATE_IDLE

    print("test_mid_process_start_into_sponge_charging passed.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_tilts_and_normal_tapping()
    test_fast_track_after_130_min_and_tapping_end()
    test_mid_process_start_into_sponge_charging()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("All state machine tests passed.")
