"""
Backfill: existing CSVs -> SQL Server
-------------------------------------
Loads every heat already sitting in `data/heats_index.csv` and
`data/heats/*.csv` into the `Heats` / `HeatEvents` tables, so the database
starts out with the full history rather than only heats recorded from the
moment the new code went live.

Safe to run as many times as you like: event keys are derived from
(heat_id, row number, event name), and every insert is guarded, so a second
run inserts nothing new. Run it again after any period where the database
was down to be certain nothing was missed.

Usage:
    python3 migrate_csv_to_sql.py            # load everything
    python3 migrate_csv_to_sql.py --dry-run  # show what would be loaded
"""

import argparse
import csv
import datetime
import glob
import os
import socket
import sys

import config
import db


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except ValueError:
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


def main():
    parser = argparse.ArgumentParser(description="Backfill heat CSVs into SQL Server.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report, but don't write to the database.")
    parser.add_argument("--data-dir", default=config.DATA_DIR)
    parser.add_argument("--index-csv", default=config.INDEX_CSV)
    args = parser.parse_args()

    host = socket.gethostname()[:128]
    index = load_index(args.index_csv)
    heat_files = sorted(glob.glob(os.path.join(args.data_dir, "*.csv")))

    print("=" * 62)
    print("CSV -> SQL SERVER BACKFILL")
    print("=" * 62)
    print(f"Index file   : {args.index_csv} ({len(index)} heat(s))")
    print(f"Heat files   : {args.data_dir} ({len(heat_files)} file(s))")
    print(f"Target       : {config.SQL_SERVER} / {config.SQL_DATABASE}")
    print(f"Mode         : {'DRY RUN - nothing will be written' if args.dry_run else 'WRITE'}")
    print("=" * 62)

    if not index and not heat_files:
        print("\nNothing to load - no CSV data found.")
        return 0

    conn = None
    cursor = None
    if not args.dry_run:
        try:
            conn = db.connect()
        except Exception as exc:
            print(f"\nFAILED to connect to SQL Server:\n  {exc}")
            print("\nFix the connection (try `python3 check_sql.py`) and re-run.")
            return 1
        cursor = conn.cursor()
        if config.SQL_AUTO_CREATE_SCHEMA:
            print("\nEnsuring tables/views exist...")
            for statement in db.schema_statements():
                try:
                    cursor.execute(statement)
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    print(f"  warning: {exc}")
            print("  done.")

    heats_written = 0
    events_written = 0

    # ---- heat summary rows ------------------------------------------
    print("\nLoading heats...")
    for heat_id, row in index.items():
        number = heat_number_from_id(heat_id)
        start = parse_dt(row.get("start_time"))
        end = parse_dt(row.get("end_time"))
        duration = parse_num(row.get("duration_minutes"))
        tilts = parse_int(row.get("tilt_count")) or 0
        status = row.get("status") or "IN_PROGRESS"
        csv_file = row.get("csv_file")

        if args.dry_run:
            print(f"  {heat_id}  {status:<12} start={start} end={end} tilts={tilts}")
        else:
            cursor.execute(db.SQL_UPSERT_HEAT_END,
                           end, duration, tilts, status, heat_id,
                           heat_id, number, start, end, duration, tilts, status,
                           csv_file, host)
        heats_written += 1

    if not args.dry_run:
        conn.commit()
    print(f"  {heats_written} heat row(s).")

    # ---- per-heat event rows ----------------------------------------
    print("\nLoading events...")
    for path in heat_files:
        heat_id = os.path.splitext(os.path.basename(path))[0]
        number = heat_number_from_id(heat_id)

        # A heat file with no matching index row still gets its events
        # loaded; make sure the parent heat row exists first so the
        # analysis view can join to it.
        if heat_id not in index and not args.dry_run:
            cursor.execute(db.SQL_UPSERT_HEAT_START,
                           number, None, os.path.basename(path), host, heat_id,
                           heat_id, number, None, os.path.basename(path), host)

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

            if args.dry_run:
                print(f"  {heat_id}[{ordinal}] {name:<24} {duration if duration is not None else '-'} min")
            else:
                cursor.execute(db.SQL_INSERT_EVENT,
                               key, heat_id, number, name, start, end,
                               duration, tilt_number, note, host, key)
            events_written += 1

        if not args.dry_run:
            conn.commit()

    print(f"  {events_written} event row(s) from {len(heat_files)} file(s).")

    if not args.dry_run:
        cursor.execute(f"SELECT COUNT(*) FROM {db.HEATS}")
        heats_total = cursor.fetchone()[0]
        cursor.execute(f"SELECT COUNT(*) FROM {db.EVENTS}")
        events_total = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        print("\n" + "=" * 62)
        print(f"Database now holds {heats_total} heat(s) and {events_total} event(s).")
        print("=" * 62)
    else:
        print("\nDry run complete - nothing was written.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
