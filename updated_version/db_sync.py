"""
CSV -> SQL Server backfill
--------------------------
Reads `data/heats_index.csv` and `data/heats/*.csv` and pushes them into
the `Heats` / `HeatEvents` tables, so the database holds the full history
rather than only the heats recorded since the SQL code went live.

Deadlock Prevention & Resilience:
---------------------------------
1. **Single Backfill Lock**: `BACKFILL_LOCK` guarantees only one backfill runs
   at a time across furnaces, preventing concurrent backfill workers from
   competing for table/page locks.
2. **One Heat Per Transaction**: Each heat is written and committed in its own
   small transaction rather than holding one massive transaction across all files.
3. **Deadlock Recovery**: On SQL Server error 1205 (deadlock), the transaction
   is immediately rolled back, the connection is closed, and a fresh connection
   retries with exponential backoff and jitter.
4. **Strict Locking Order**: Writes always touch `dbo.Heats` first, then
   `dbo.HeatEvents` second, eliminating cyclic lock hierarchy deadlocks.
5. **Incremental Sync**: Already synced completed heats are queried once and
   skipped, avoiding redundant writes and locks on historic data.
6. **Resource Cleanup**: Every cursor and connection is guaranteed to close
   in `finally` blocks.
"""

import csv
import datetime
import glob
import logging
import os
import random
import socket
import threading
import time

try:
    import pyodbc
except ImportError:
    pyodbc = None

import config
import db

logger = logging.getLogger(__name__)

# Global lock to ensure only one backfill operation touches the database
# across all background threads and furnace instances at a time.
BACKFILL_LOCK = threading.Lock()

# Populated by the background thread(s) so /api/sql/status can report on
# them. Keyed by furnace slug (e.g. "4-1", "4-2") so two furnaces backfilling
# don't stomp on each other's status / thread handle.
_last_results = {}
_lock = threading.Lock()
_threads = {}


# ---------------------------------------------------------------------------
# Deadlock retry helper
# ---------------------------------------------------------------------------
def is_deadlock_error(exc):
    """Check if an exception represents SQL Server deadlock (error 1205)."""
    text = str(exc).lower()
    return "1205" in text or "deadlock" in text


def run_with_deadlock_retry(operation, max_retries=5, logger_func=None):
    """Run operation() with retries on SQL Server deadlock (error 1205).

    operation: zero-argument callable that manages its own connection/transaction
    lifecycle. On error 1205, rolls back and closes its connection, sleeps with
    exponential backoff + random jitter, and retries with a fresh connection.
    """
    log = logger_func or logger.warning
    for attempt in range(1, max_retries + 1):
        try:
            return operation()
        except Exception as exc:
            if not is_deadlock_error(exc):
                raise

            if attempt >= max_retries:
                logger.exception("SQL deadlock persists after %d attempts. Giving up.", max_retries)
                raise

            delay = min(2 ** (attempt - 1) + random.uniform(0.2, 1.0), 10.0)
            log(
                "SQL deadlock detected (error 1205). Attempt %d/%d. Retrying in %.1f seconds.",
                attempt, max_retries, delay
            )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def heat_number_from_id(heat_id):
    parts = (heat_id or "").split("_")
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def load_index(index_csv):
    """heat_id -> summary dict, from heats_index.csv."""
    if not os.path.exists(index_csv):
        return {}
    with open(index_csv, newline="") as f:
        return {row["heat_id"]: row for row in csv.DictReader(f) if row.get("heat_id")}


# ---------------------------------------------------------------------------
# Incremental sync: check already-synced heats
# ---------------------------------------------------------------------------
def get_synced_completed_heat_ids(furnace_id):
    """Fetch set of heat_ids for completed heats already stored in SQL Server.

    A heat is considered already synced if:
      1. It exists in dbo.Heats with heat_status = 'COMPLETE' for this furnace.
      2. It has at least one event record in dbo.HeatEvents.
    """
    def _fetch():
        conn = cursor = None
        try:
            conn = db.connect()
            cursor = conn.cursor()
            query = f"""
            SELECT H.heat_id
              FROM {db.HEATS} H
             WHERE H.FurnaceId = ?
               AND H.heat_status = 'COMPLETE'
               AND EXISTS (SELECT 1 FROM {db.EVENTS} E WHERE E.heat_id = H.heat_id)
            """
            cursor.execute(query, furnace_id)
            return {row[0] for row in cursor.fetchall()}
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    try:
        return run_with_deadlock_retry(_fetch, max_retries=3)
    except Exception as exc:
        logger.warning("Could not fetch already synced heats for %s (will sync all): %s", furnace_id, exc)
        return set()


# ---------------------------------------------------------------------------
# Schema setup helper
# ---------------------------------------------------------------------------
def _ensure_schema():
    """Create schema tables and indexes if they do not exist yet."""
    def _create():
        conn = cursor = None
        try:
            conn = db.connect()
            cursor = conn.cursor()
            for statement in db.schema_statements():
                try:
                    cursor.execute(statement)
                    conn.commit()
                except Exception as exc:
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    logger.warning("Schema statement failed (continuing): %s", exc)
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    return run_with_deadlock_retry(_create, max_retries=3)


# ---------------------------------------------------------------------------
# Single Heat Sync (One Transaction, Heats First -> HeatEvents Second)
# ---------------------------------------------------------------------------
def _sync_one_heat(heat_id, row, events_csv_path, furnace_id, dry_run=False, emit=None):
    """Sync a single heat to SQL Server.

    Guarantees:
      1. Always updates dbo.Heats FIRST.
      2. Always updates dbo.HeatEvents SECOND.
      3. Exactly one heat per transaction.
      4. On failure: rolls back, closes cursor & connection, and raises so
         run_with_deadlock_retry can reconnect and retry.
    """
    emit = emit or logger.info
    furnace, stand = db.parse_furnace_stand(furnace_id)
    number = heat_number_from_id(heat_id)

    # Read events from CSV if file exists
    event_rows = []
    if events_csv_path and os.path.exists(events_csv_path):
        try:
            with open(events_csv_path, newline="") as f:
                event_rows = list(csv.DictReader(f))
        except Exception as exc:
            logger.warning("Could not read events CSV for %s (%s): %s", heat_id, events_csv_path, exc)

    if dry_run:
        status_desc = row.get("status") if row else "FROM_FILE"
        emit(f"  [DRY RUN] {heat_id} ({status_desc}): {len(event_rows)} event(s)")
        return 1, len(event_rows)

    def _do_sync():
        conn = cursor = None
        try:
            conn = db.connect()
            cursor = conn.cursor()

            # -------------------------------------------------------------
            # STEP 1: Always update dbo.Heats FIRST
            # -------------------------------------------------------------
            if row is not None:
                start = parse_dt(row.get("start_time"))
                end = parse_dt(row.get("end_time"))
                duration = parse_num(row.get("duration_minutes"))
                status = row.get("status") or "IN_PROGRESS"

                duration_str = db._str_duration(duration)
                scrap_dur_str = db._str_duration(row.get("scrap_duration"))
                sponge_dur_str = db._str_duration(row.get("sponge_duration"))
                tapping_dur_str = db._str_duration(row.get("tapping_duration"))

                cursor.execute(
                    db.SQL_UPSERT_HEAT_END,
                    # UPDATE (12 params)
                    furnace, stand, furnace_id, number, status, start, end,
                    duration_str, scrap_dur_str, sponge_dur_str, tapping_dur_str, heat_id,
                    # INSERT (12 params)
                    heat_id, furnace, stand, furnace_id, number, status, start, end,
                    duration_str, scrap_dur_str, sponge_dur_str, tapping_dur_str,
                )
            else:
                # Standalone CSV file with no index row
                cursor.execute(
                    db.SQL_UPSERT_HEAT_START,
                    # UPDATE (6 params)
                    furnace, stand, furnace_id, number, None, heat_id,
                    # INSERT (6 params)
                    heat_id, furnace, stand, furnace_id, number, None,
                )

            # -------------------------------------------------------------
            # STEP 2: Always update dbo.HeatEvents SECOND
            # -------------------------------------------------------------
            events_written = 0
            for ordinal, erow in enumerate(event_rows):
                name = (erow.get("event") or "").strip()
                if not name:
                    continue
                key = db.make_event_key(heat_id, ordinal, name)
                estart = parse_dt(erow.get("start_time"))
                eend = parse_dt(erow.get("end_time"))
                eduration = parse_num(erow.get("duration_minutes"))
                eduration_str = db._str_duration(eduration)
                estatus = "COMPLETED" if eend is not None else "IN_PROGRESS"

                cursor.execute(
                    db.SQL_UPSERT_EVENT,
                    # UPDATE (8 params)
                    heat_id, furnace_id, name, estart, eend, eduration_str, estatus, key,
                    # INSERT (8 params)
                    key, heat_id, furnace_id, name, estart, eend, eduration_str, estatus,
                )
                events_written += 1

            # -------------------------------------------------------------
            # STEP 3: Commit this single heat's transaction
            # -------------------------------------------------------------
            conn.commit()
            return 1, events_written

        except Exception:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    return run_with_deadlock_retry(_do_sync, max_retries=5)


# ---------------------------------------------------------------------------
# Internal Backfill Implementation
# ---------------------------------------------------------------------------
def _backfill_database_internal(data_dir=None, index_csv=None, furnace_id=None, dry_run=False,
                                create_schema=None, skip_heat_ids=(), emit=None, force=False):
    data_dir = data_dir or config.DATA_DIR
    index_csv = index_csv or config.INDEX_CSV
    furnace_id = furnace_id or config.FURNACE_ID
    create_schema = config.SQL_AUTO_CREATE_SCHEMA if create_schema is None else create_schema
    emit = emit or logger.info

    index = load_index(index_csv)
    heat_files = {os.path.splitext(os.path.basename(p))[0]: p
                  for p in sorted(glob.glob(os.path.join(data_dir, "*.csv")))}
    skip = set(skip_heat_ids or ())

    result = {
        "state": "running",
        "dry_run": dry_run,
        "furnace_id": furnace_id,
        "heats_seen": len(index),
        "files_seen": len(heat_files),
        "heats_written": 0,
        "events_written": 0,
        "heats_skipped": 0,
        "skipped": sorted(skip),
        "error": None,
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }

    emit(f"Backfill: {len(index)} indexed heat(s), {len(heat_files)} heat file(s) "
         f"-> {config.SQL_SERVER}/{config.SQL_DATABASE} (FurnaceId={furnace_id})"
         f"{' [DRY RUN]' if dry_run else ''}")

    if not index and not heat_files:
        result.update(state="done", note="nothing to load")
        result["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        return result

    try:
        # Step 1: Ensure schema if requested
        if not dry_run and create_schema:
            _ensure_schema()

        # Step 2: Check already synced heats to avoid re-writing 55 files every cycle
        already_synced = set()
        if not dry_run and not force:
            already_synced = get_synced_completed_heat_ids(furnace_id)
            if already_synced:
                logger.info("Found %d already synced completed heat(s) in SQL Server for %s.",
                            len(already_synced), furnace_id)

        # Step 3: Collect ordered list of heats to process
        all_heat_ids = list(index.keys())
        for hid in heat_files:
            if hid not in index:
                all_heat_ids.append(hid)

        # Step 4: Process heat by heat
        for heat_id in all_heat_ids:
            # Live heat being recorded right now
            if heat_id in skip:
                continue

            index_row = index.get(heat_id)
            is_completed = (index_row and index_row.get("status") == "COMPLETE") or (index_row is None and heat_id in already_synced)

            # Skip completed heats that are already in SQL Server
            if not force and is_completed and heat_id in already_synced:
                result["heats_skipped"] += 1
                continue

            csv_path = heat_files.get(heat_id) or os.path.join(data_dir, f"{heat_id}.csv")
            hw, ew = _sync_one_heat(heat_id, index_row, csv_path, furnace_id,
                                    dry_run=dry_run, emit=emit)
            result["heats_written"] += hw
            result["events_written"] += ew

        # Step 5: Read back row counts for reporting (informational only)
        if not dry_run:
            conn = cursor = None
            try:
                conn = db.connect()
                cursor = conn.cursor()
                cursor.execute(f"SELECT COUNT(*) FROM {db.HEATS} WHERE FurnaceId = ?", furnace_id)
                result["heats_total"] = cursor.fetchone()[0]
                cursor.execute(f"SELECT COUNT(*) FROM {db.EVENTS} WHERE FurnaceId = ?", furnace_id)
                result["events_total"] = cursor.fetchone()[0]
            except Exception as exc:
                logger.debug("Could not read back row counts: %s", exc)
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        result["state"] = "done"
        emit(f"Backfill complete for {furnace_id}: {result['heats_written']} heat(s) written, "
             f"{result['events_written']} event(s) written, {result['heats_skipped']} already synced.")

    except Exception as exc:
        result.update(state="failed", error=str(exc)[:500])
        logger.warning("Backfill failed for %s: %s", furnace_id, exc)
        raise
    finally:
        result["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")

    return result


# ---------------------------------------------------------------------------
# Public Synchronous Backfill
# ---------------------------------------------------------------------------
def backfill(data_dir=None, index_csv=None, furnace_id=None, dry_run=False, create_schema=None,
             skip_heat_ids=(), emit=None, force=False, lock_timeout=120.0):
    """Synchronous backfill guarded by BACKFILL_LOCK.

    Only one backfill operation may execute across all furnaces/threads at a time.
    If another backfill holds the lock, waits up to lock_timeout seconds.
    If lock_timeout is reached, logs and returns a skipped status.
    """
    acquired = BACKFILL_LOCK.acquire(blocking=True, timeout=lock_timeout)
    if not acquired:
        logger.info("Previous backfill is still running; skipping this cycle for furnace %s.", furnace_id)
        return {
            "state": "skipped",
            "note": "backfill already running (lock timeout/busy)",
            "furnace_id": furnace_id or config.FURNACE_ID,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    try:
        return _backfill_database_internal(
            data_dir=data_dir,
            index_csv=index_csv,
            furnace_id=furnace_id,
            dry_run=dry_run,
            create_schema=create_schema,
            skip_heat_ids=skip_heat_ids,
            emit=emit,
            force=force,
        )
    finally:
        BACKFILL_LOCK.release()


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------
def status(key=None):
    """Snapshot of the background backfill, for the status API.

    key: furnace slug (e.g. "4-1"). Omit to get a dict of every furnace's
    status keyed by slug (used by a combined status view)."""
    with _lock:
        if key is None:
            return {k: dict(v) for k, v in _last_results.items()}
        return dict(_last_results.get(key, {"state": "not_started"}))


def _set_status(key, **kw):
    with _lock:
        _last_results.setdefault(key, {}).update(kw)


def _worker(key, data_dir, index_csv, furnace_id, state_machine, max_attempts, retry_seconds):
    # A heat that is live right now must not be overwritten by the stale
    # CSV row for it; the recorder owns that row until the heat completes.
    skip = set()
    if state_machine is not None and getattr(state_machine, "heat_id", None):
        skip.add(state_machine.heat_id)

    delay = retry_seconds
    for attempt in range(1, max_attempts + 1):
        _set_status(key, state="running", attempt=attempt)
        try:
            result = backfill(data_dir=data_dir, index_csv=index_csv,
                              furnace_id=furnace_id, skip_heat_ids=skip)
            result["attempt"] = attempt
            _set_status(key, **result)
            if result.get("state") == "skipped":
                if attempt < max_attempts:
                    logger.info("Backfill for %s was skipped (busy), retrying in %ss", key, delay)
                    time.sleep(delay)
                    delay = min(delay * 2, config.SQL_RETRY_MAX_SECONDS)
                    continue
            return
        except Exception as exc:
            _set_status(key, state="retrying", attempt=attempt, error=str(exc)[:500])
            if attempt == max_attempts:
                logger.warning(
                    "Backfill for %s gave up after %d attempt(s): %s. "
                    "Live heats are still being recorded (and spooled if the "
                    "database is down); run `python3 migrate_csv_to_sql.py "
                    "--furnace-id %s` once it's reachable to load the history.",
                    key, attempt, exc, furnace_id)
                _set_status(key, state="failed")
                return
            logger.info("Backfill for %s attempt %d failed, retrying in %ss", key, attempt, delay)
            time.sleep(delay)
            delay = min(delay * 2, config.SQL_RETRY_MAX_SECONDS)


def start_background_backfill(key, data_dir=None, index_csv=None, furnace_id=None,
                              state_machine=None, max_attempts=5, retry_seconds=None):
    """Kick the CSV backfill off on its own daemon thread and return
    immediately. Never raises - a database problem here must not stop the
    dashboard or the camera from starting.

    key: furnace slug (e.g. "4-1", "4-2") - keeps this furnace's thread and
    status separate from any other furnace backfilling at the same time."""
    if not config.SQL_ENABLED:
        _set_status(key, state="disabled")
        return None
    if not config.SQL_BACKFILL_ON_START:
        _set_status(key, state="skipped")
        return None
    existing = _threads.get(key)
    if existing is not None and existing.is_alive():
        return existing

    thread = threading.Thread(
        target=_worker,
        args=(key,
              data_dir or config.DATA_DIR,
              index_csv or config.INDEX_CSV,
              furnace_id,
              state_machine,
              max_attempts,
              retry_seconds or config.SQL_RETRY_MIN_SECONDS),
        name=f"SqlBackfill-{key}",
        daemon=True,
    )
    _threads[key] = thread
    thread.start()
    logger.info("CSV -> SQL backfill started in background thread for %s.", key)
    return thread