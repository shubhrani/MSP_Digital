"""
Exercises the full heat cycle logic, including the four behaviours added
on top of the original scrap -> sponge -> tapping flow:

  1. Tilt-count cooldown (2 minutes) so flickery tilt/normal cycles don't
     inflate the count.
  2. The 130-minutes-of-sponge-charging "fast track" into tapping (any of
     12/13/16/18), backdated by 2:13.
  3. Tapping / the heat ending ~15 minutes after tapping starts.
  4. magnet_feeding_scrap (class 3) ending tapping early and starting the
     next heat, when it happens before the 15-minute mark.

Run with:

    python3 tests/test_state_machine.py

No third-party dependencies required.
"""

import csv
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_machine import HeatStateMachine, TAPPING_BACKDATE, TAPPING_MAX_DURATION  # noqa: E402

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
def test_tilt_cooldown_and_normal_tapping():
    """Heat 1: two flickery tilts inside the cooldown window that must NOT
    count, a couple of tilts spaced apart that DO count, then a normal
    (under-130-minute) tapping confirmation via class 16, ended by the
    15-minute timer."""
    sm = fresh_machine()

    sm.process_detections({3}, t(0))          # scrap charging starts
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.heat_id == "Heat_001_20260811_080000"

    sm.process_detections({8}, t(25))         # scrap ends, sponge starts
    assert sm.state == sm.STATE_SPONGE_CHARGING

    # First tilt: nothing counted yet, so it always counts.
    sm.process_detections({12}, t(70))
    assert sm.furnace_watch_active is True
    sm.process_detections({2}, t(71))
    assert sm.furnace_watch_active is False
    assert sm.tilt_count == 1

    # Second cycle resolves only 1.5 min after the first counted tilt ->
    # inside the 2-minute cooldown -> must NOT increment the count.
    sm.process_detections({18}, t(72))
    sm.process_detections({2}, t(72.5))
    assert sm.tilt_count == 1

    # Third cycle resolves 3.2 min after the last *counted* tilt (t(71))
    # -> outside the cooldown -> counts.
    sm.process_detections({12}, t(74))
    sm.process_detections({2}, t(74.2))
    assert sm.tilt_count == 2

    # Real tapping sequence, still well under 130 min of sponge charging
    # (74.2 min in): 18 -> 16 confirms tapping immediately.
    sm.process_detections({18}, t(100))
    assert sm.furnace_watch_active is True
    sm.process_detections({16}, t(101))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(100)

    # No class-3 / no class-2 needed anymore: the 15-minute timer ends it.
    # Check a little after the boundary to prove the logged time is
    # capped exactly at tapping_start + 15 min, not whenever it's checked.
    sm.check_timeouts(t(100) + TAPPING_MAX_DURATION + datetime.timedelta(minutes=1))
    assert sm.state == sm.STATE_IDLE

    heat1_csv = read_csv(sm.get_heat_csv_path("Heat_001_20260811_080000"))
    events = {row["event"]: row for row in heat1_csv if row["event"] not in ("tilt", "tilt_ignored_cooldown")}
    tilts = [row for row in heat1_csv if row["event"] == "tilt"]
    ignored = [row for row in heat1_csv if row["event"] == "tilt_ignored_cooldown"]

    assert float(events["scrap_charging"]["duration_minutes"]) == 25.0
    # sponge charging measured from t(25) to t(100) = 75 min
    assert float(events["sponge_charging"]["duration_minutes"]) == 75.0
    # tapping capped at exactly 15 min, regardless of when it was checked
    assert float(events["tapping"]["duration_minutes"]) == 15.0
    assert events["tapping"]["end_time"] == (t(100) + TAPPING_MAX_DURATION).isoformat()

    assert len(tilts) == 2
    assert tilts[0]["tilt_number"] == "1"
    assert tilts[1]["tilt_number"] == "2"
    assert len(ignored) == 1

    index_rows = sm.get_heats_history()
    assert len(index_rows) == 1
    assert index_rows[0]["status"] == "COMPLETE"
    assert index_rows[0]["tilt_count"] == "2"

    # Heat 2 starts cleanly, numbering continues, tilt count resets.
    sm.process_detections({3}, t(200))
    assert sm.heat_id == "Heat_002_20260811_080000".replace("080000", t(200).strftime("%H%M%S"))
    assert sm.tilt_count == 0
    assert sm.last_tilt_time is None

    print("test_tilt_cooldown_and_normal_tapping passed.")


# ---------------------------------------------------------------------------
def test_fast_track_after_130_min_and_early_end_via_magnet():
    """A heat where sponge charging runs past 130 minutes: any of
    12/13/16/18 should immediately confirm tapping (no need to also see
    class 2), backdated by 2:13. Then magnet_feeding_scrap arrives before
    the 15-minute mark, ending tapping early and starting the next heat
    from that same moment."""
    sm = fresh_machine()

    sm.process_detections({3}, t(0))
    sm.process_detections({8}, t(10))          # sponge_start = t(10)
    assert sm.state == sm.STATE_SPONGE_CHARGING

    # 135 minutes into sponge charging (>= 130) - class 18 fires.
    detect_time = t(10 + 135)
    sm.process_detections({18}, detect_time)

    boundary = detect_time - TAPPING_BACKDATE
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == boundary
    assert sm.furnace_watch_active is False  # fast track bypasses the watch

    heat_csv_path = sm.get_heat_csv_path(sm.heat_id)
    rows = read_csv(heat_csv_path)
    sponge_row = next(r for r in rows if r["event"] == "sponge_charging")
    assert sponge_row["end_time"] == boundary.isoformat()
    expected_sponge_minutes = round((boundary - t(10)).total_seconds() / 60.0, 2)
    assert float(sponge_row["duration_minutes"]) == expected_sponge_minutes

    # magnet_feeding_scrap arrives ~7 minutes after tapping started - well
    # before the 15-minute mark - so it ends this heat immediately and
    # starts the next one from this same timestamp.
    magnet_time = boundary + datetime.timedelta(minutes=7)
    old_heat_id = sm.heat_id
    sm.process_detections({3}, magnet_time)

    assert sm.state == sm.STATE_SCRAP_CHARGING  # new heat already started
    assert sm.heat_id != old_heat_id
    assert sm.scrap_start == magnet_time

    old_rows = read_csv(sm.get_heat_csv_path(old_heat_id))
    tapping_row = next(r for r in old_rows if r["event"] == "tapping")
    assert tapping_row["end_time"] == magnet_time.isoformat()
    expected_tap_minutes = round((magnet_time - boundary).total_seconds() / 60.0, 2)
    assert float(tapping_row["duration_minutes"]) == expected_tap_minutes

    index_rows = {r["heat_id"]: r for r in sm.get_heats_history()}
    assert index_rows[old_heat_id]["status"] == "COMPLETE"

    print("test_fast_track_after_130_min_and_early_end_via_magnet passed.")


# ---------------------------------------------------------------------------
def test_timer_wins_when_magnet_detected_after_the_15min_mark():
    """If magnet_feeding_scrap only shows up after the 15-minute mark has
    already passed, the timer should have closed the heat first (capped at
    exactly +15 min), and the magnet detection then starts a fresh heat
    from its own timestamp - not before."""
    sm = fresh_machine()

    sm.process_detections({3}, t(0))
    sm.process_detections({8}, t(5))
    sm.process_detections({18}, t(10))
    sm.process_detections({16}, t(11))         # tapping starts at t(10)
    assert sm.state == sm.STATE_TAPPING
    old_heat_id = sm.heat_id
    tapping_start = sm.tapping_start

    # Fires 20 minutes after tapping started - 5 min past the 15-min mark -
    # in a single process_detections call, with no prior check_timeouts.
    late_time = tapping_start + datetime.timedelta(minutes=20)
    sm.process_detections({3}, late_time)

    old_rows = read_csv(sm.get_heat_csv_path(old_heat_id))
    tapping_row = next(r for r in old_rows if r["event"] == "tapping")
    assert tapping_row["end_time"] == (tapping_start + TAPPING_MAX_DURATION).isoformat()
    assert float(tapping_row["duration_minutes"]) == 15.0

    # The magnet detection still starts a brand-new heat, from its own
    # (later) timestamp, since by the time it's handled the old heat had
    # already been closed by the timer.
    assert sm.heat_id != old_heat_id
    assert sm.state == sm.STATE_SCRAP_CHARGING
    assert sm.scrap_start == late_time

    print("test_timer_wins_when_magnet_detected_after_the_15min_mark passed.")


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

    # A tilt/tap sequence from here on behaves exactly as normal.
    sm.process_detections({18}, t(30))
    sm.process_detections({16}, t(31))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(30)

    print("test_mid_process_start_into_sponge_charging passed.")


# ---------------------------------------------------------------------------
def test_mid_process_start_into_tapping():
    """App is (re)started with the furnace bottom already fully visible
    (class 16) and nothing tracked yet - the clearest possible signal that
    tapping is already underway. Both earlier stages should be logged as
    unknown, and the heat should land straight in TAPPING."""
    sm = fresh_machine()

    sm.process_detections({16}, t(0))
    assert sm.state == sm.STATE_TAPPING
    assert sm.tapping_start == t(0)
    assert sm.scrap_start is None
    assert sm.sponge_start is None

    rows = read_csv(sm.get_heat_csv_path(sm.heat_id))
    logged_events = [r["event"] for r in rows]
    assert logged_events == ["scrap_charging_unknown", "sponge_charging_unknown"]

    # 15-minute timer still ends it normally from here.
    sm.check_timeouts(t(0) + TAPPING_MAX_DURATION + datetime.timedelta(minutes=1))
    assert sm.state == sm.STATE_IDLE

    print("test_mid_process_start_into_tapping passed.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_tilt_cooldown_and_normal_tapping()
    test_fast_track_after_130_min_and_early_end_via_magnet()
    test_timer_wins_when_magnet_detected_after_the_15min_mark()
    test_mid_process_start_into_sponge_charging()
    test_mid_process_start_into_tapping()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("All state machine tests passed.")
