#!/usr/bin/env python3
"""
purge_long_heats.py
===================

Delete every heat whose duration is longer than a threshold (default 300
minutes), across ALL furnaces and ALL stands.

Rules
-----
* If both heat_start_time and heat_end_time are populated, duration is
  computed as DATEDIFF(MINUTE, heat_start_time, heat_end_time).
* Otherwise the script falls back to the stored heat_duration text column
  (parsed as a float).  Non-numeric / blank values are ignored.
* For every heat that matches, its rows in dbo.HeatEvents are deleted
  first, then the row in dbo.Heats -- matching the admin app's own
  delete order (no FK is declared in the schema).

Safety
------
* Runs in PREVIEW mode by default.  Nothing is deleted until you pass
  --apply.
* Each individual heat is deleted inside its own transaction, so a
  failure on one heat does not roll back the ones already deleted.
* Every deleted heat_id is echoed to the console and (optionally) to a
  log file so you have an audit trail.

Usage
-----
    python purge_long_heats.py                 # preview only
    python purge_long_heats.py --apply         # actually delete
    python purge_long_heats.py --apply --minutes 300
    python purge_long_heats.py --apply --furnace 4/1        # narrow it
    python purge_long_heats.py --apply --stand SL1          # narrow it
    python purge_long_heats.py --apply --log purge.log
"""

import argparse
import datetime
import sys

import config
import db


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=300.0,
                   help="Duration threshold in minutes (default: 300).")
    p.add_argument("--apply", action="store_true",
                   help="Actually delete. Without this flag the script only previews.")
    p.add_argument("--furnace", default=None,
                   help="Restrict to one FurnaceId (default: all).")
    p.add_argument("--stand", default=None,
                   help="Restrict to one stand (default: all).")
    p.add_argument("--log", default=None,
                   help="Optional path to a log file for the deleted heat_ids.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
# We compute the effective duration in SQL so the filter is done on the
# server, and we never pull back more than we need.
_FIND_SQL = """
SELECT
    heat_id,
    FurnaceId,
    stand,
    heat_start_time,
    heat_end_time,
    heat_duration,
    CASE
        WHEN heat_start_time IS NOT NULL AND heat_end_time IS NOT NULL
            THEN DATEDIFF(MINUTE, heat_start_time, heat_end_time)
        ELSE TRY_CAST(heat_duration AS FLOAT)
    END AS effective_minutes
FROM {heats}
WHERE
    (
        (heat_start_time IS NOT NULL AND heat_end_time IS NOT NULL
         AND DATEDIFF(MINUTE, heat_start_time, heat_end_time) > ?)
        OR
        (heat_start_time IS NULL OR heat_end_time IS NULL)
         AND TRY_CAST(heat_duration AS FLOAT) > ?
    )
    {furnace_clause}
    {stand_clause}
ORDER BY effective_minutes DESC, heat_id
"""


def find_long_heats(conn, threshold, furnace=None, stand=None):
    where_furnace = ""
    where_stand = ""
    params = [threshold, threshold]

    if furnace:
        where_furnace = "AND FurnaceId = ?"
        params.append(furnace)
    if stand:
        where_stand = "AND stand = ?"
        params.append(stand)

    sql = _FIND_SQL.format(
        heats=db.HEATS,
        furnace_clause=where_furnace,
        stand_clause=where_stand,
    )
    cur = conn.cursor()
    cur.execute(sql, *params)
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def delete_heat(conn, heat_id):
    """Delete one heat + its events inside a single transaction.  Returns
    (n_events_deleted, n_heats_deleted)."""
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {db.EVENTS} WHERE heat_id = ?", heat_id)
    n_events = cur.rowcount
    cur.execute(f"DELETE FROM {db.HEATS}  WHERE heat_id = ?", heat_id)
    n_heats = cur.rowcount
    conn.commit()
    return n_events, n_heats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()

    if not config.SQL_ENABLED:
        print("ERROR: config.SQL_ENABLED is 0.  Set SQL_ENABLED=1 to run this.")
        sys.exit(2)

    print("=" * 70)
    print("Purge long heats")
    print("=" * 70)
    print(f"SQL server   : {config.SQL_SERVER}")
    print(f"SQL database : {config.SQL_DATABASE}")
    print(f"SQL login    : {config.SQL_UID}")
    print(f"Threshold    : > {args.minutes} minutes")
    print(f"Furnace      : {args.furnace or '(any)'}")
    print(f"Stand        : {args.stand or '(any)'}")
    print(f"Mode         : {'APPLY (deleting)' if args.apply else 'PREVIEW (nothing will be deleted)'}")
    print("=" * 70)

    log_fp = open(args.log, "a", encoding="utf-8") if args.log else None
    if log_fp:
        log_fp.write(f"\n--- run at {datetime.datetime.now().isoformat()} "
                     f"threshold={args.minutes} apply={args.apply} ---\n")

    conn = db.connect()
    try:
        rows = find_long_heats(conn, args.minutes, args.furnace, args.stand)
        if not rows:
            print("No heats matched.  Nothing to do.")
            return

        print(f"{len(rows)} heat(s) matched:\n")
        print(f"{'heat_id':<24} {'FurnaceId':<10} {'stand':<10} "
              f"{'start':<20} {'end':<20} {'mins':>8}")
        print("-" * 100)
        for heat_id, furnace_id, stand, start, end, _dur, mins in rows:
            print(f"{str(heat_id):<24} {str(furnace_id or ''):<10} "
                  f"{str(stand or ''):<10} "
                  f"{str(start or ''):<20} {str(end or ''):<20} "
                  f"{mins if mins is not None else '?':>8}")
        print("-" * 100)

        if not args.apply:
            print("\nPREVIEW only.  Re-run with --apply to actually delete.")
            return

        print("\nDeleting...\n")
        deleted_heats = 0
        deleted_events = 0
        failed = 0
        for heat_id, *_ in rows:
            try:
                n_events, n_heats = delete_heat(conn, heat_id)
                deleted_heats += n_heats
                deleted_events += n_events
                line = (f"deleted heat_id={heat_id!r}  "
                        f"({n_heats} heat row, {n_events} event rows)")
                print("  " + line)
                if log_fp:
                    log_fp.write(line + "\n")
            except Exception as exc:
                failed += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
                err = f"FAILED heat_id={heat_id!r}: {exc}"
                print("  " + err, file=sys.stderr)
                if log_fp:
                    log_fp.write(err + "\n")

        print("\n" + "=" * 70)
        print(f"Heats deleted : {deleted_heats}")
        print(f"Events deleted: {deleted_events}")
        if failed:
            print(f"Failures      : {failed}")
        print("=" * 70)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if log_fp:
            log_fp.close()


if __name__ == "__main__":
    main()