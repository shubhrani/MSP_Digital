"""Tests for db_sync deadlock prevention, locking, retries, and table ordering."""

import csv
import datetime
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_sync
import db

TMP_DIR = os.path.join(os.path.dirname(__file__), "_tmp_db_sync")


def setup_tmp_csvs():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    heats_dir = os.path.join(TMP_DIR, "heats")
    os.makedirs(heats_dir, exist_ok=True)

    index_csv = os.path.join(TMP_DIR, "heats_index.csv")
    with open(index_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["heat_id", "start_time", "end_time", "duration_minutes", "tilt_count", "status", "csv_file"])
        writer.writerow(["Heat_001_20260824_100000", "2026-08-24 10:00:00", "2026-08-24 11:30:00", "90.0", "1", "COMPLETE", "Heat_001_20260824_100000.csv"])
        writer.writerow(["Heat_002_20260824_120000", "2026-08-24 12:00:00", "2026-08-24 13:30:00", "90.0", "0", "COMPLETE", "Heat_002_20260824_120000.csv"])

    heat1_csv = os.path.join(heats_dir, "Heat_001_20260824_100000.csv")
    with open(heat1_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["event", "start_time", "end_time", "duration_minutes", "tilt_number", "note"])
        writer.writerow(["scrap_charging", "2026-08-24 10:00:00", "2026-08-24 10:20:00", "20.0", "", ""])
        writer.writerow(["sponge_charging", "2026-08-24 10:20:00", "2026-08-24 11:15:00", "55.0", "", ""])
        writer.writerow(["tapping", "2026-08-24 11:15:00", "2026-08-24 11:30:00", "15.0", "", ""])

    heat2_csv = os.path.join(heats_dir, "Heat_002_20260824_120000.csv")
    with open(heat2_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["event", "start_time", "end_time", "duration_minutes", "tilt_number", "note"])
        writer.writerow(["scrap_charging", "2026-08-24 12:00:00", "2026-08-24 12:20:00", "20.0", "", ""])

    return heats_dir, index_csv


def test_is_deadlock_error():
    class FakeDeadlockError(Exception):
        pass

    e1 = FakeDeadlockError("Transaction (Process ID 55) was deadlocked on lock resources with another process. SQL Server error 1205")
    assert db_sync.is_deadlock_error(e1)

    e2 = Exception("Deadlock encountered during write")
    assert db_sync.is_deadlock_error(e2)

    e3 = Exception("Login failed for user 'sa'")
    assert not db_sync.is_deadlock_error(e3)
    print("test_is_deadlock_error passed.")


def test_run_with_deadlock_retry_recovers():
    calls = []

    def flaky_operation():
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise Exception("SQL Server error 1205: Deadlock victim")
        return "SUCCESS"

    result = db_sync.run_with_deadlock_retry(flaky_operation, max_retries=5)
    assert result == "SUCCESS"
    assert len(calls) == 3
    print("test_run_with_deadlock_retry_recovers passed.")


def test_run_with_deadlock_retry_aborts_on_non_deadlock():
    calls = []

    def non_deadlock_op():
        calls.append(1)
        raise ValueError("Invalid column name")

    try:
        db_sync.run_with_deadlock_retry(non_deadlock_op, max_retries=5)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    assert len(calls) == 1
    print("test_run_with_deadlock_retry_aborts_on_non_deadlock passed.")


def test_table_order_and_transaction_commit():
    """Mock connection that records table access order and commit/rollback calls."""
    heats_dir, index_csv = setup_tmp_csvs()

    executed_statements = []
    committed_count = [0]
    rollbacked_count = [0]
    closed_connections = [0]

    class FakeCursor:
        def execute(self, sql, *params):
            sql_clean = " ".join(sql.split())
            if sql_clean.startswith("SELECT"):
                return
            if db.EVENTS in sql_clean:
                executed_statements.append(("HeatEvents", sql_clean[:30]))
            elif db.HEATS in sql_clean:
                executed_statements.append(("Heats", sql_clean[:30]))
            elif "SELECT H.heat_id" in sql_clean:
                pass
            elif "SELECT COUNT(*)" in sql_clean:
                pass

        def fetchall(self):
            return []

        def fetchone(self):
            return (0,)

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            committed_count[0] += 1

        def rollback(self):
            rollbacked_count[0] += 1

        def close(self):
            closed_connections[0] += 1

    old_connect = db.connect
    db.connect = lambda: FakeConn()
    try:
        result = db_sync._backfill_database_internal(
            data_dir=heats_dir,
            index_csv=index_csv,
            furnace_id="4/1",
            create_schema=False,
            force=True,  # sync all without skipping
            today_only=False,  # sync all for this transaction order test
        )
        assert result["heats_written"] == 2
        assert result["events_written"] == 4

        # Verify commit per heat (one transaction per heat)
        # 2 heats synced = 2 commits
        assert committed_count[0] == 2

        # Verify consistent order: Heats is ALWAYS touched before HeatEvents
        # executed_statements should show [('Heats', ...), ('HeatEvents', ...), ('Heats', ...), ('HeatEvents', ...)]
        order = [tbl for tbl, _ in executed_statements]
        assert order == ["Heats", "HeatEvents", "HeatEvents", "HeatEvents", "Heats", "HeatEvents"]
        print("test_table_order_and_transaction_commit passed.")
    finally:
        db.connect = old_connect


def test_skips_already_synced_heats():
    """Verify that completed heats in SQL Server are skipped without writes."""
    heats_dir, index_csv = setup_tmp_csvs()

    executed = []

    class FakeCursor:
        def execute(self, sql, *params):
            if "SELECT H.heat_id" in sql:
                return
            executed.append(sql)

        def fetchall(self):
            # Heat_001 is already completed and synced in SQL Server
            return [("Heat_001_20260824_100000",)]

        def fetchone(self):
            return (1,)

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    old_connect = db.connect
    db.connect = lambda: FakeConn()
    try:
        result = db_sync._backfill_database_internal(
            data_dir=heats_dir,
            index_csv=index_csv,
            furnace_id="4/1",
            create_schema=False,
            force=False,
            today_only=False,
        )
        # Heat_001 was skipped, Heat_002 was written
        assert result["heats_skipped"] == 1
        assert result["heats_written"] == 1
        assert result["events_written"] == 1
        print("test_skips_already_synced_heats passed.")
    finally:
        db.connect = old_connect


def test_today_only_filters_previous_heats():
    """Verify that historical heats from past dates are filtered out when today_only is enabled."""
    heats_dir, index_csv = setup_tmp_csvs()

    executed = []
    class FakeCursor:
        def execute(self, sql, *params):
            executed.append(sql)
        def fetchall(self):
            return []
        def fetchone(self):
            return (0,)
        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    old_connect = db.connect
    db.connect = lambda: FakeConn()
    try:
        # Default behavior: today_only is True
        result = db_sync._backfill_database_internal(
            data_dir=heats_dir,
            index_csv=index_csv,
            furnace_id="4/1",
            create_schema=False,
            today_only=True,
        )
        # Both heats in setup_tmp_csvs are from 2026-08-24 (past date)
        assert result["heats_filtered_prior_date"] == 2
        assert result["heats_written"] == 0
        assert result["events_written"] == 0
        # No writes executed
        assert not any("INSERT" in sql or "UPDATE" in sql for sql in executed)
        print("test_today_only_filters_previous_heats passed.")
    finally:
        db.connect = old_connect


def test_today_only_includes_todays_heats():
    """Verify that heats from today are synced, while past heats are filtered out."""
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    heats_dir = os.path.join(TMP_DIR, "heats")
    os.makedirs(heats_dir, exist_ok=True)

    today_str = datetime.date.today().strftime("%Y%m%d")
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y%m%d")

    today_heat_id = f"Heat_010_{today_str}_100000"
    yesterday_heat_id = f"Heat_009_{yesterday_str}_100000"

    index_csv = os.path.join(TMP_DIR, "heats_index.csv")
    with open(index_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["heat_id", "start_time", "end_time", "duration_minutes", "tilt_count", "status", "csv_file"])
        writer.writerow([yesterday_heat_id, f"{yesterday.isoformat()} 10:00:00", f"{yesterday.isoformat()} 11:00:00", "60.0", "0", "COMPLETE", f"{yesterday_heat_id}.csv"])
        writer.writerow([today_heat_id, f"{datetime.date.today().isoformat()} 10:00:00", f"{datetime.date.today().isoformat()} 11:00:00", "60.0", "0", "COMPLETE", f"{today_heat_id}.csv"])

    with open(os.path.join(heats_dir, f"{yesterday_heat_id}.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["event", "start_time", "end_time", "duration_minutes", "tilt_number", "note"])
        writer.writerow(["scrap_charging", f"{yesterday.isoformat()} 10:00:00", f"{yesterday.isoformat()} 10:20:00", "20.0", "", ""])

    with open(os.path.join(heats_dir, f"{today_heat_id}.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["event", "start_time", "end_time", "duration_minutes", "tilt_number", "note"])
        writer.writerow(["scrap_charging", f"{datetime.date.today().isoformat()} 10:00:00", f"{datetime.date.today().isoformat()} 10:20:00", "20.0", "", ""])

    executed = []
    class FakeCursor:
        def execute(self, sql, *params):
            executed.append((sql, params))
        def fetchall(self):
            return []
        def fetchone(self):
            return (0,)
        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass

    old_connect = db.connect
    db.connect = lambda: FakeConn()
    try:
        result = db_sync._backfill_database_internal(
            data_dir=heats_dir,
            index_csv=index_csv,
            furnace_id="4/1",
            create_schema=False,
            today_only=True,
        )
        assert result["heats_filtered_prior_date"] == 1
        assert result["heats_written"] == 1
        assert result["events_written"] == 1
        print("test_today_only_includes_todays_heats passed.")
    finally:
        db.connect = old_connect


def test_backfill_lock_serializes_concurrent_calls():
    """Verify that BACKFILL_LOCK guarantees only one backfill runs at a time."""
    heats_dir, index_csv = setup_tmp_csvs()
    active_backfills = [0]
    max_concurrent = [0]

    old_internal = db_sync._backfill_database_internal

    def mock_internal(**kwargs):
        active_backfills[0] += 1
        if active_backfills[0] > max_concurrent[0]:
            max_concurrent[0] = active_backfills[0]
        time.sleep(0.05)
        active_backfills[0] -= 1
        return {"state": "done", "heats_written": 0, "events_written": 0}

    db_sync._backfill_database_internal = mock_internal
    try:
        t1 = threading.Thread(target=lambda: db_sync.backfill(data_dir=heats_dir, index_csv=index_csv, furnace_id="4/1", dry_run=True, today_only=False))
        t2 = threading.Thread(target=lambda: db_sync.backfill(data_dir=heats_dir, index_csv=index_csv, furnace_id="4/2", dry_run=True, today_only=False))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Both ran, but max concurrent was strictly 1
        assert max_concurrent[0] == 1
        print("test_backfill_lock_serializes_concurrent_calls passed.")
    finally:
        db_sync._backfill_database_internal = old_internal


if __name__ == "__main__":
    test_is_deadlock_error()
    test_run_with_deadlock_retry_recovers()
    test_run_with_deadlock_retry_aborts_on_non_deadlock()
    test_table_order_and_transaction_commit()
    test_skips_already_synced_heats()
    test_today_only_filters_previous_heats()
    test_today_only_includes_todays_heats()
    test_backfill_lock_serializes_concurrent_calls()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("\nAll db_sync deadlock prevention and date filter tests passed successfully!")

