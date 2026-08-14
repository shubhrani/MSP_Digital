"""
Backfill: existing CSVs -> SQL Server (command line)
----------------------------------------------------
Loads every heat already sitting in `data/heats_index.csv` and
`data/heats/*.csv` into the `Heats` / `HeatEvents` tables.

The app now does this for you on startup, in a background thread
(see db_sync.start_background_backfill, wired up in app.py). This script
is the manual version - useful for a first load, for a dry run, or after
a period where the database was down.

Safe to run as many times as you like: event keys are derived from
(heat_id, row number, event name), and every insert is guarded, so a
second run inserts nothing new.

Usage:
    python3 migrate_csv_to_sql.py            # load everything
    python3 migrate_csv_to_sql.py --dry-run  # show what would be loaded
"""

import argparse
import sys

import config
import db_sync


def main():
    parser = argparse.ArgumentParser(description="Backfill heat CSVs into SQL Server.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report, but don't write to the database.")
    parser.add_argument("--furnace-id", choices=sorted({f["furnace_id"] for f in config.FURNACES.values()}),
                        default=config.FURNACE_ID,
                        help="Which furnace these CSVs belong to (default: %(default)s). "
                             "Picks that furnace's own data-dir/index-csv unless overridden below.")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--index-csv", default=None)
    args = parser.parse_args()

    # Resolve data-dir/index-csv from the chosen furnace unless the caller
    # gave explicit paths.
    furnace_cfg = next((f for f in config.FURNACES.values() if f["furnace_id"] == args.furnace_id),
                       None)
    data_dir = args.data_dir or (furnace_cfg["data_dir"] if furnace_cfg else config.DATA_DIR)
    index_csv = args.index_csv or (furnace_cfg["index_csv"] if furnace_cfg else config.INDEX_CSV)

    print("=" * 62)
    print("CSV -> SQL SERVER BACKFILL")
    print("=" * 62)
    print(f"Furnace      : {args.furnace_id}")
    print(f"Index file   : {index_csv}")
    print(f"Heat files   : {data_dir}")
    print(f"Target       : {config.SQL_SERVER} / {config.SQL_DATABASE}")
    print(f"Mode         : {'DRY RUN - nothing will be written' if args.dry_run else 'WRITE'}")
    print("=" * 62)

    try:
        result = db_sync.backfill(data_dir=data_dir,
                                  index_csv=index_csv,
                                  furnace_id=args.furnace_id,
                                  dry_run=args.dry_run,
                                  emit=print)
    except Exception as exc:
        print(f"\nFAILED: {exc}")
        print("\nFix the connection (try `python3 check_sql.py`) and re-run.")
        return 1

    print("\n" + "=" * 62)
    if args.dry_run:
        print("Dry run complete - nothing was written.")
    else:
        print(f"Loaded {result['heats_written']} heat row(s) and "
              f"{result['events_written']} event row(s).")
        if "heats_total" in result:
            print(f"Database now holds {result['heats_total']} heat(s) and "
                  f"{result['events_total']} event(s).")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
