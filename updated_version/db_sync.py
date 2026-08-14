"""
CSV -> SQL Server backfill
--------------------------
Reads `data/heats_index.csv` and `data/heats/*.csv` and pushes them into
the `Heats` / `HeatEvents` tables, so the database holds the full history
rather than only the heats recorded since the SQL code went live.

Two ways in:

  backfill(...)                  synchronous, used by migrate_csv_to_sql.py
  start_background_backfill(...) fire-and-forget daemon thread, used by app.py

The background version is the important one for the dashboard. Opening an
ODBC connection is a blocking network call - on a slow link or an
unreachable server it can sit there for the full login timeout, and every
INSERT after it is another round trip. Doing that on the main thread at
startup would stall Flask before it ever binds its port, and doing it on
the camera thread would drop frames. So it gets a thread of its own, the
detection loop never waits on it, and if the database is down the app
comes up normally and the backfill simply retries in the background.

Idempotent: event keys are derived from (heat_id, row number, event name)
and every insert is guarded, so running it twice inserts nothing new.
"""

import csv
import datetime
import glob
import logging
import os
import socket
import threading
import time

import config
import db

logger = logging.getLogger(__name__)

# Populated by the background thread(s) so /api/sql/status can report on
# them. Keyed by furnace slug (e.g. "4-1", "4-2") so two furnaces backfilling
# at once don't stomp on each other's status / thread handle.
_last_results = {}
_lock = threading.Lock()
_threads = {}


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
# Synchronous backfill
# ---------------------------------------------------------------------------
def backfill(data_dir=None, index_csv=None, furnace_id=None, dry_run=False, create_schema=None,
             skip_heat_ids=(), emit=None):
    """Load every CSV heat into SQL Server. Returns a summary dict.

    furnace_id: which furnace these CSVs belong to (e.g. "4/1" or "4/2").
    Defaults to config.FURNACE_ID for backward compatibility with callers
    that only ever dealt with one furnace.

    skip_heat_ids: heats to leave alone - used to keep a heat that is
    running *right now* from having its live IN_PROGRESS row overwritten
    by the stale CSV snapshot taken when this thread started.

    emit: called with each progress line (default: the module logger).
    """
    data_dir = data_dir or config.DATA_DIR
    index_csv = index_csv or config.INDEX_CSV
    furnace_id = furnace_id or config.FURNACE_ID
    create_schema = config.SQL_AUTO_CREATE_SCHEMA if create_schema is None else create_schema
    emit = emit or logger.info

    host = socket.gethostname()[:128]
    index = load_index(index_csv)
    heat_files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    skip = set(skip_heat_ids or ())

    result = {
        "state": "running",
        "dry_run": dry_run,
        "furnace_id": furnace_id,
        "heats_seen": len(index),
        "files_seen": len(heat_files),
        "heats_written": 0,
        "events_written": 0,
        "skipped": sorted(skip),
        "error": None,
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }

    emit(f"Backfill: {len(index)} indexed heat(s), {len(heat_files)} heat file(s) "
         f"-> {config.SQL_SERVER}/{config.SQL_DATABASE} (FurnaceId={furnace_id})"
         f"{' [DRY RUN]' if dry_run else ''}")

    if not index and not heat_files:
        result.update(state="done", note="nothing to load")
        return result

    conn = cursor = None
    try:
        if not dry_run:
            conn = db.connect()
            cursor = conn.cursor()
            if create_schema:
                for statement in db.schema_statements():
                    try:
                        cursor.execute(statement)
                        conn.commit()
                    except Exception as exc:
                        conn.rollback()
                        logger.warning("Schema statement failed (continuing): %s", exc)

        # ---- heat summary rows --------------------------------------
        for heat_id, row in index.items():
            if heat_id in skip:
                continue
            number = heat_number_from_id(heat_id)
            start = parse_dt(row.get("start_time"))
            end = parse_dt(row.get("end_time"))
            duration = parse_num(row.get("duration_minutes"))
            tilts = parse_int(row.get("tilt_count")) or 0
            status = row.get("status") or "IN_PROGRESS"
            csv_file = row.get("csv_file")

            if dry_run:
                emit(f"  {heat_id}  {status:<12} start={start} end={end} tilts={tilts}")
            else:
                cursor.execute(db.SQL_UPSERT_HEAT_END,
                               furnace_id, end, duration, tilts, status, heat_id,
                               heat_id, furnace_id, number, start, end, duration,
                               tilts, status, csv_file, host)
            result["heats_written"] += 1

        if not dry_run:
            conn.commit()

        # ---- per-heat event rows ------------------------------------
        for path in heat_files:
            heat_id = os.path.splitext(os.path.basename(path))[0]
            if heat_id in skip:
                continue
            number = heat_number_from_id(heat_id)

            # A heat file with no matching index row still gets its events
            # loaded; make sure the parent heat row exists first so the
            # analysis view can join to it.
            if heat_id not in index and not dry_run:
                cursor.execute(db.SQL_UPSERT_HEAT_START,
                               furnace_id, number, None, os.path.basename(path), host, heat_id,
                               heat_id, furnace_id, number, None, os.path.basename(path), host)

            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))

            for ordinal, row in enumerate(rows):
                name = (row.get("event") or "").strip()
                if not name:
                    continue
                key = db.make_event_key(heat_id, ordinal, name)
                start = parse_dt(row.get("start_time"))
                end = parse_dt(row.get("end_time"))
                duration = parse_num(row.get("duration_minutes"))
                tilt_number = parse_int(row.get("tilt_number"))
                note = (row.get("note") or None)  # older CSVs have no note column

                if dry_run:
                    emit(f"  {heat_id}[{ordinal}] {name:<24} "
                         f"{duration if duration is not None else '-'} min")
                else:
                    cursor.execute(db.SQL_UPSERT_EVENT,
                                   # UPDATE
                                   start, end, duration, tilt_number, note, host, key,
                                   # INSERT
                                   key, heat_id, furnace_id, number, name,
                                   start, end, duration, tilt_number, note, host)
                result["events_written"] += 1

            if not dry_run:
                conn.commit()

        # Purely informational, and deliberately not allowed to fail the
        # run: the rows are already committed by this point, so a hiccup
        # counting them mustn't send us back around the retry loop and
        # redo the whole load.
        if not dry_run:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {db.HEATS}")
                result["heats_total"] = cursor.fetchone()[0]
                cursor.execute(f"SELECT COUNT(*) FROM {db.EVENTS}")
                result["events_total"] = cursor.fetchone()[0]
            except Exception as exc:
                logger.debug("Could not read back row counts: %s", exc)

        result["state"] = "done"
        emit(f"Backfill complete: {result['heats_written']} heat row(s), "
             f"{result['events_written']} event row(s).")

    except Exception as exc:
        result.update(state="failed", error=str(exc)[:500])
        logger.warning("Backfill failed: %s", exc)
        raise
    finally:
        result["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
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

    return result


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