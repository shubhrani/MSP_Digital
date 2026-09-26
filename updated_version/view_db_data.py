"""
view_db_data.py
---------------
Utility script to inspect the SQL Server database schema and data:
1. Lists all tables and views in the database with row counts.
2. Lists all columns, data types, and nullability for each table/view.
3. Displays recent heats and process events.

Run with:
    python view_db_data.py
"""

import json
import sys

import config
import db


def format_data_type(data_type, char_len, num_prec, num_scale):
    """Format SQL Server data type into a human-readable string like VARCHAR(64)."""
    dt = str(data_type).upper()
    if dt in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "BINARY", "VARBINARY"):
        if char_len == -1:
            return f"{dt}(MAX)"
        elif char_len is not None:
            return f"{dt}({char_len})"
    elif dt in ("DECIMAL", "NUMERIC"):
        if num_prec is not None and num_scale is not None:
            return f"{dt}({num_prec},{num_scale})"
    return dt


def print_database_schema(conn):
    """Query and print all tables, views, and their columns from INFORMATION_SCHEMA."""
    cursor = conn.cursor()

    # 1. Fetch all tables and views
    cursor.execute("""
        SELECT 
            TABLE_SCHEMA,
            TABLE_NAME,
            TABLE_TYPE
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA NOT IN ('sys')
        ORDER BY TABLE_TYPE, TABLE_SCHEMA, TABLE_NAME;
    """)
    tables = cursor.fetchall()

    if not tables:
        print("No user tables or views found in this database.")
        return

    print("\n" + "=" * 80)
    print("DATABASE TABLES & VIEWS")
    print("=" * 80)

    # 2. Fetch row counts for each table
    print(f"{'Schema':<10} {'Name':<30} {'Type':<15} {'Rows':<10}")
    print("-" * 80)
    for schema, name, tbl_type in tables:
        row_count_str = "-"
        if tbl_type == "BASE TABLE":
            try:
                cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{name}]")
                count = cursor.fetchone()[0]
                row_count_str = f"{count:,}"
            except Exception:
                row_count_str = "Error"
        print(f"{schema:<10} {name:<30} {tbl_type:<15} {row_count_str:<10}")

    # 3. Fetch all columns grouped by table
    cursor.execute("""
        SELECT 
            TABLE_SCHEMA,
            TABLE_NAME,
            ORDINAL_POSITION,
            COLUMN_NAME,
            DATA_TYPE,
            CHARACTER_MAXIMUM_LENGTH,
            NUMERIC_PRECISION,
            NUMERIC_SCALE,
            IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA NOT IN ('sys')
        ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
    """)
    columns = cursor.fetchall()

    # Group columns by (schema, table_name)
    table_columns = {}
    for row in columns:
        key = (row.TABLE_SCHEMA, row.TABLE_NAME)
        table_columns.setdefault(key, []).append(row)

    print("\n" + "=" * 80)
    print("DETAILED TABLE & COLUMN DEFINITIONS")
    print("=" * 80)

    for schema, name, tbl_type in tables:
        key = (schema, name)
        cols = table_columns.get(key, [])
        kind = "Table" if tbl_type == "BASE TABLE" else "View"
        print(f"\n[{kind}] {schema}.{name} ({len(cols)} columns):")
        print(f"  {'#':<4} {'Column Name':<32} {'Data Type':<20} {'Nullable':<10}")
        print("  " + "-" * 68)

        for col in cols:
            formatted_type = format_data_type(
                col.DATA_TYPE,
                col.CHARACTER_MAXIMUM_LENGTH,
                col.NUMERIC_PRECISION,
                col.NUMERIC_SCALE,
            )
            nullable_str = "NULL" if col.IS_NULLABLE == "YES" else "NOT NULL"
            print(f"  {col.ORDINAL_POSITION:<4} {col.COLUMN_NAME:<32} {formatted_type:<20} {nullable_str:<10}")


def main():
    print("=" * 80)
    print("FURNACE PROCESS MONITOR - SQL DATABASE INSPECTOR")
    print("=" * 80)
    print("Server   :", config.SQL_SERVER)
    print("Database :", config.SQL_DATABASE)
    print("UID      :", config.SQL_UID)
    print("Driver   :", config.SQL_DRIVER or "Auto-detect")

    print("\nConnecting to database...")
    status = db.test_connection()
    if not status.get("ok"):
        print("FAILED to connect to database:", status.get("error"))
        print("\nNote: Ensure SQL Server is running, network is reachable, and pyodbc is installed.")
        sys.exit(1)

    print(f"SUCCESS: Connected to '{status.get('database')}' as '{status.get('login')}'")
    print(f"SQL Server Version: {status.get('version')}")

    conn = None
    try:
        conn = db.connect()

        # 1. Print all tables, views, and columns
        print_database_schema(conn)

        # 2. Print recent heats
        print("\n" + "=" * 80)
        print("RECENT HEATS (Top 5 from vw_HeatAnalysis / Heats)")
        print("=" * 80)
        try:
            heats = db.fetch_recent_heats(limit=5)
            if not heats:
                print("No heats found in the database.")
            else:
                for h in heats:
                    print(json.dumps(h, indent=2, default=str))
        except Exception as e:
            print("Could not fetch heats from view:", e)

        # 3. Print recent events
        print("\n" + "=" * 80)
        print(f"RECENT EVENTS (Top 5 from {db.EVENTS})")
        print("=" * 80)
        try:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT TOP 5 
                    id, 
                    heat_id, 
                    event_id, 
                    FurnaceId, 
                    event_name, 
                    start_time, 
                    end_time, 
                    duration, 
                    status 
                FROM {db.EVENTS} 
                ORDER BY start_time DESC
            """)
            col_names = [c[0] for c in cursor.description]
            rows = cursor.fetchall()
            if not rows:
                print("No events found in HeatEvents table.")
            else:
                for row in rows:
                    record = {col_names[i]: (str(val) if val is not None else None) for i, val in enumerate(row)}
                    print(json.dumps(record, indent=2))
        except Exception as e:
            print("Could not fetch events:", e)

    except Exception as exc:
        print("\nAn error occurred while querying the database:", exc)
    finally:
        if conn:
            conn.close()

    print("\n" + "=" * 80)
    print("DATABASE INSPECTION COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
