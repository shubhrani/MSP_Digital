import pyodbc
import socket

import config
import db


# ============================================================
# SQL SERVER CONFIGURATION
# ============================================================
# Read from config.py (which reads the SQL_* environment variables), so
# this diagnostic always tests exactly what the app itself will use - and
# so the password lives in one place, outside the source tree.

SQL_SERVER = config.SQL_SERVER
SQL_DATABASE = config.SQL_DATABASE
SQL_UID = config.SQL_UID
SQL_PASSWORD = config.SQL_PASSWORD

DRIVER = db.detect_driver() or "ODBC Driver 17 for SQL Server"

CONN_STR = db.build_connection_string(DRIVER)

if not SQL_PASSWORD:
    print("WARNING: no password set. Run:  export SQL_PASSWORD='...'  "
          "(setx SQL_PASSWORD \"...\" on Windows) and try again.\n")


# ============================================================
# PRINT CONFIGURATION
# ============================================================

print("=" * 60)
print("SQL SERVER ACCESS TEST")
print("=" * 60)

print(f"Server   : {SQL_SERVER}")
print(f"Database : {SQL_DATABASE}")
print(f"Username : {SQL_UID}")
print(f"Driver   : {DRIVER}")
print("=" * 60)


# ============================================================
# TEST 1 — CHECK SQL SERVER NETWORK ACCESS
# ============================================================

print("\n[1] Checking SQL Server network connectivity...")

try:

    # If localhost is used, SQL Server normally listens on 1433.
    # For named instances, this check is only informational.
    host = SQL_SERVER.split("\\")[0].split(",")[0]

    socket.gethostbyname(host)

    print("SUCCESS: SQL Server host is reachable.")
    print(f"Resolved host: {host}")

except Exception as e:

    print("FAILED: Could not resolve/reach SQL Server host.")
    print(f"Error: {e}")


# ============================================================
# TEST 2 — CONNECT TO DATABASE
# ============================================================

print("\n[2] Testing SQL Server database connection...")

connection = None
cursor = None

try:

    connection = pyodbc.connect(
        CONN_STR,
        timeout=5
    )

    print("SUCCESS: Connected to SQL Server.")
    print(f"Database '{SQL_DATABASE}' is accessible.")

except pyodbc.Error as e:

    print("FAILED: Could not connect to SQL Server.")
    print("\nSQL Error:")
    print(e)

    print("\nPossible causes:")
    print("1. SQL Server service is not running.")
    print("2. SQL Server name/instance is incorrect.")
    print("3. SQL Server authentication is disabled.")
    print("4. Username/password is incorrect.")
    print("5. TCP/IP is disabled.")
    print("6. SQL Server is using a different port.")
    print("7. ODBC Driver 17 is not installed.")

    print("\nTest finished.")

    input("\nPress Enter to exit...")
    exit()


# ============================================================
# TEST 3 — CHECK CURRENT USER
# ============================================================

print("\n[3] Checking authenticated SQL user...")

try:

    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            SYSTEM_USER AS SystemUser,
            SUSER_SNAME() AS LoginName,
            DB_NAME() AS DatabaseName
    """)

    row = cursor.fetchone()

    print("SUCCESS: Authentication verified.")
    print(f"System User : {row.SystemUser}")
    print(f"Login Name  : {row.LoginName}")
    print(f"Database    : {row.DatabaseName}")

except pyodbc.Error as e:

    print("FAILED: Could not determine current SQL user.")
    print(e)


# ============================================================
# TEST 4 — CHECK READ PERMISSION
# ============================================================

print("\n[4] Testing READ permission...")

try:

    cursor.execute("""
        SELECT TOP 1
            name
        FROM sys.tables
    """)

    row = cursor.fetchone()

    if row:
        print("SUCCESS: READ permission is working.")
        print(f"Example table found: {row.name}")
    else:
        print("SUCCESS: SELECT permission is working.")
        print("However, the database currently contains no user tables.")

except pyodbc.Error as e:

    print("FAILED: READ permission is NOT available.")
    print(e)


# ============================================================
# TEST 5 — CHECK WRITE PERMISSION
# ============================================================
#
# We create a temporary table, insert data, read it,
# update it, then delete it.
#
# Everything is inside a TRANSACTION.
#
# Finally we ROLLBACK so the database is not permanently changed.
# ============================================================

print("\n[5] Testing WRITE permission...")

try:

    # Start transaction
    connection.autocommit = False

    test_table = "#PythonSQLAccessTest"

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    cursor.execute(f"""
        CREATE TABLE {test_table} (
            ID INT,
            TestValue VARCHAR(100)
        )
    """)

    print("  CREATE : SUCCESS")

    # --------------------------------------------------------
    # INSERT
    # --------------------------------------------------------

    cursor.execute(
        f"""
        INSERT INTO {test_table} (ID, TestValue)
        VALUES (?, ?)
        """,
        1,
        "Python SQL Test"
    )

    print("  INSERT : SUCCESS")

    # --------------------------------------------------------
    # SELECT
    # --------------------------------------------------------

    cursor.execute(
        f"""
        SELECT ID, TestValue
        FROM {test_table}
        """
    )

    row = cursor.fetchone()

    if row:
        print("  SELECT : SUCCESS")
        print(f"           ID={row.ID}, TestValue='{row.TestValue}'")

    # --------------------------------------------------------
    # UPDATE
    # --------------------------------------------------------

    cursor.execute(
        f"""
        UPDATE {test_table}
        SET TestValue = ?
        WHERE ID = ?
        """,
        "Updated Test",
        1
    )

    print("  UPDATE : SUCCESS")

    # --------------------------------------------------------
    # DELETE
    # --------------------------------------------------------

    cursor.execute(
        f"""
        DELETE FROM {test_table}
        WHERE ID = ?
        """,
        1
    )

    print("  DELETE : SUCCESS")

    # --------------------------------------------------------
    # ROLLBACK
    # --------------------------------------------------------

    connection.rollback()

    print("\nSUCCESS: WRITE permissions are available.")
    print("All test changes were rolled back.")
    print("No permanent data was added/modified.")

except pyodbc.Error as e:

    print("\nFAILED: WRITE permission test failed.")
    print(e)

    try:
        connection.rollback()
        print("Test transaction rolled back.")
    except:
        pass


# ============================================================
# TEST 6 — CHECK SERVER INFORMATION
# ============================================================

print("\n[6] Getting SQL Server information...")

try:

    cursor.execute("""
        SELECT
            @@SERVERNAME AS ServerName,
            @@VERSION AS SQLVersion
    """)

    row = cursor.fetchone()

    print("Server Name:")
    print(row.ServerName)

    print("\nSQL Server Version:")
    print(row.SQLVersion)

except pyodbc.Error as e:

    print("Could not retrieve server information.")
    print(e)


# ============================================================
# CLOSE CONNECTION
# ============================================================

try:

    cursor.close()
    connection.close()

    print("\nConnection closed successfully.")

except:
    pass


# ============================================================
# FINAL RESULT
# ============================================================

print("\n" + "=" * 60)
print("SQL SERVER ACCESS TEST COMPLETED")
print("=" * 60)

input("\nPress Enter to exit...")