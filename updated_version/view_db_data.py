import sys
import json
import db
import config

def main():
    print("--- Database Configuration ---")
    print(f"Server: {config.SQL_SERVER}")
    print(f"Database: {config.SQL_DATABASE}")
    print(f"UID: {config.SQL_UID}")
    print(f"Driver: {config.SQL_DRIVER or 'Auto-detect'}")
    
    print("\nTesting connection...")
    status = db.test_connection()
    if not status.get("ok"):
        print("Failed to connect to the database:", status.get("error"))
        sys.exit(1)
        
    print(f"Connected successfully to {status.get('database')} as {status.get('login')}")
    print(f"Version: {status.get('version')}")
    
    print("\n--- Recent Heats (from vw_HeatAnalysis) ---")
    try:
        heats = db.fetch_recent_heats(limit=5)
        if not heats:
            print("No heats found in the database.")
        else:
            for h in heats:
                print(json.dumps(h, indent=2))
    except Exception as e:
        print("Error fetching heats:", e)
        
    print("\n--- Recent Events (from HeatEvents table) ---")
    conn = None
    try:
        conn = db.connect()
        cursor = conn.cursor()
        cursor.execute(f"SELECT TOP 5 EventKey, HeatId, EventName, StartTime, EndTime, DurationMinutes FROM {db.EVENTS} ORDER BY StartTime DESC")
        columns = [c[0] for c in cursor.description]
        rows = cursor.fetchall()
        if not rows:
            print("No events found.")
        else:
            for row in rows:
                # Convert values for display
                record = {}
                for name, value in zip(columns, row):
                    record[name] = str(value) if value is not None else None
                print(json.dumps(record, indent=2))
    except Exception as e:
        print("Error fetching events:", e)
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    main()
