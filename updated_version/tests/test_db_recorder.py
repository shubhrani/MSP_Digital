"""
Exercises the SQL Server recording path without needing a real database:

  1. The state machine hands every CSV row to the recorder as well, with
     the right heat/event/duration values.
  2. Rows are spooled to disk when the database is unreachable, and
     replayed in order once it comes back.
  3. Event keys are deterministic, so a replay (or a re-run of
     migrate_csv_to_sql.py) can't create duplicates.
  4. With no recorder attached at all, nothing changes - CSV only.

A fake pyodbc is injected into db.py, so this runs anywhere.

Run with:

    python3 tests/test_db_recorder.py
"""

import datetime
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from state_machine import HeatStateMachine  # noqa: E402

TMP_DIR = os.path.join(os.path.dirname(__file__), "_tmp_db")
BASE = datetime.datetime(2026, 8, 11, 8, 0, 0)


def t(minutes_offset):
    return BASE + datetime.timedelta(minutes=minutes_offset)


def fresh_tmp():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(TMP_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class RecordingRecorder:
    """Stands in for db.SqlRecorder and just remembers the calls."""

    def __init__(self):
        self.calls = []

    def record_heat_started(self, **kw):
        self.calls.append(("heat_start", kw))

    def record_heat_completed(self, **kw):
        self.calls.append(("heat_end", kw))

    def record_event(self, **kw):
        self.calls.append(("event", kw))

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *params):
        if self.conn.fail:
            raise RuntimeError("simulated database outage")
        # Guards the easiest mistake to make in this file: a statement and
        # its argument list drifting out of sync.
        placeholders = sql.count("?")
        assert placeholders == len(params), (
            f"{placeholders} placeholder(s) but {len(params)} argument(s):\n{sql[:200]}")
        self.conn.executed.append((sql.strip()[:40], params))
        return self

    def close(self):
        pass


class FakeConnection:
    def __init__(self, registry):
        self.registry = registry
        self.executed = registry["executed"]
        self.autocommit = False
        self.timeout = 0

    @property
    def fail(self):
        return self.registry["fail"]

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.fail:
            raise RuntimeError("simulated database outage")

    def rollback(self):
        pass

    def close(self):
        pass


class FakePyodbc:
    def __init__(self, registry):
        self.registry = registry

    def drivers(self):
        return ["ODBC Driver 17 for SQL Server"]

    def connect(self, conn_str, timeout=None):
        if self.registry["fail"]:
            raise RuntimeError("simulated connect failure")
        self.registry["connects"] += 1
        return FakeConnection(self.registry)


def install_fake_pyodbc():
    registry = {"fail": False, "executed": [], "connects": 0}
    db.pyodbc = FakePyodbc(registry)
    return registry


# ---------------------------------------------------------------------------
def test_state_machine_feeds_the_recorder():
    """Every CSV row should reach the recorder with matching values."""
    fresh_tmp()
    rec = RecordingRecorder()
    sm = HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                          index_csv=os.path.join(TMP_DIR, "heats_index.csv"),
                          recorder=rec)

    sm.process_detections({3}, t(0))            # heat starts
    sm.process_detections({8}, t(25))           # scrap -> sponge
    sm.process_detections({12}, t(70))          # tilt begins (< 130 min sponge)
    sm.process_detections({2}, t(71))           # tilt resolves, counted
    sm.process_detections({12}, t(160))         # >= 130 min sponge -> tapping starts
    sm.process_detections({2}, t(175))          # furnace back to normal -> tapping ends

    starts = rec.of("heat_start")
    assert len(starts) == 1, starts
    assert starts[0]["heat_id"] == "Heat_001_20260811_080000"
    assert starts[0]["start_time"] == t(0)
    assert starts[0]["heat_number"] == 1

    events = rec.of("event")
    by_name = {e["event_name"]: e for e in events}
    assert by_name["scrap_charging"]["duration_minutes"] == 25.0
    assert by_name["scrap_charging"]["start_time"] == t(0)
    assert by_name["sponge_charging"]["duration_minutes"] == 135.0
    assert by_name["tapping"]["duration_minutes"] == 15.0
    assert by_name["tilt"]["tilt_number"] == 1
    # The tapping row carries why the heat ended.
    assert "normal" in (by_name["tapping"]["note"] or "")

    # Ordinals must match the row order in the CSV, since that's what
    # makes the event keys line up with a later CSV backfill. Only rows
    # that actually land in the CSV count here - the "started" placeholder
    # notifications (end_time is None) intentionally reuse the ordinal of
    # the completed row that follows them, so the two resolve to the same
    # DB key.
    logged = [e for e in events if e["end_time"] is not None]
    assert [e["ordinal"] for e in logged] == list(range(len(logged)))

    ends = rec.of("heat_end")
    assert len(ends) == 1
    assert ends[0]["status"] == "COMPLETE"
    assert ends[0]["tilt_count"] == 1
    assert ends[0]["end_time"] == t(175)
    assert ends[0]["duration_minutes"] == 175.0   # t(0) -> t(175)

    print("test_state_machine_feeds_the_recorder passed.")


def test_no_recorder_is_csv_only():
    """Leaving the recorder off must not change existing behaviour."""
    fresh_tmp()
    sm = HeatStateMachine(data_dir=os.path.join(TMP_DIR, "heats"),
                          index_csv=os.path.join(TMP_DIR, "heats_index.csv"))
    sm.process_detections({3}, t(0))
    sm.process_detections({8}, t(25))
    assert sm.recorder is None
    assert os.path.exists(sm.get_heat_csv_path(sm.heat_id))
    print("test_no_recorder_is_csv_only passed.")


def test_deterministic_event_keys():
    a = db.make_event_key("Heat_001_x", 3, "tilt")
    b = db.make_event_key("Heat_001_x", 3, "tilt")
    c = db.make_event_key("Heat_001_x", 4, "tilt")
    assert a == b, "same event must always produce the same key"
    assert a != c, "different rows must produce different keys"
    assert len(a) == 36
    print("test_deterministic_event_keys passed.")


def test_spool_and_replay():
    """Database down -> rows go to disk. Database back -> rows replay in
    order and the spool file is cleared."""
    fresh_tmp()
    registry = install_fake_pyodbc()
    spool = os.path.join(TMP_DIR, "spool.jsonl")

    recorder = db.SqlRecorder(enabled=True, spool_file=spool)
    registry["fail"] = True          # database is unreachable from the start
    recorder.start()

    recorder.record_heat_started("Heat_001", 1, t(0), csv_file="Heat_001.csv")
    for i, name in enumerate(["scrap_charging", "sponge_charging", "tapping"]):
        recorder.record_event("Heat_001", 1, name, t(i), t(i + 10),
                              duration_minutes=10.0, ordinal=i)
    recorder.record_heat_completed("Heat_001", 1, t(0), t(60), 60.0, 2)

    deadline = time.time() + 10
    while time.time() < deadline and recorder.status()["spooled"] < 5:
        time.sleep(0.05)

    status = recorder.status()
    assert status["spooled"] == 5, status
    assert status["connected"] is False
    assert status["rows_written"] == 0

    with open(spool) as f:
        spooled = [json.loads(line) for line in f if line.strip()]
    assert [row["op"] for row in spooled] == [
        "heat_start", "event", "event", "event", "heat_end"], spooled

    # Database comes back. The recorder retries on its idle tick, but the
    # backoff timer may not have expired yet, so clear it for the test.
    registry["fail"] = False
    recorder._next_retry_at = 0

    deadline = time.time() + 15
    while time.time() < deadline and recorder.status()["spooled"] > 0:
        time.sleep(0.1)

    status = recorder.status()
    assert status["spooled"] == 0, status
    assert status["rows_replayed"] == 5, status
    assert status["connected"] is True
    assert not os.path.exists(spool), "drained spool file should be removed"

    # Schema bootstrap ran, then the five data statements.
    data_statements = [sql for sql, _ in registry["executed"]
                       if sql.startswith(("UPDATE", "INSERT"))]
    assert len(data_statements) == 5, data_statements

    recorder.stop(timeout=5)
    print("test_spool_and_replay passed.")


def test_writes_go_straight_through_when_healthy():
    fresh_tmp()
    registry = install_fake_pyodbc()
    spool = os.path.join(TMP_DIR, "spool2.jsonl")

    recorder = db.SqlRecorder(enabled=True, spool_file=spool)
    recorder.start()
    recorder.record_heat_started("Heat_002", 2, t(0))
    recorder.record_event("Heat_002", 2, "scrap_charging", t(0), t(20),
                          duration_minutes=20.0, ordinal=0)

    deadline = time.time() + 10
    while time.time() < deadline and recorder.status()["rows_written"] < 2:
        time.sleep(0.05)

    status = recorder.status()
    assert status["rows_written"] == 2, status
    assert status["spooled"] == 0
    assert status["connected"] is True
    assert status["last_error"] is None
    assert not os.path.exists(spool), "nothing should be spooled while healthy"

    recorder.stop(timeout=5)
    print("test_writes_go_straight_through_when_healthy passed.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _real_pyodbc = db.pyodbc
    try:
        test_state_machine_feeds_the_recorder()
        test_no_recorder_is_csv_only()
        test_deterministic_event_keys()
        test_spool_and_replay()
        test_writes_go_straight_through_when_healthy()
    finally:
        db.pyodbc = _real_pyodbc
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("All recorder tests passed.")
