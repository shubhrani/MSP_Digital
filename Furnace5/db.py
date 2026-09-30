"""
SQL Server recorder
-------------------
Everything the app used to write only to CSV is now also pushed to SQL
Server, in a shape that's actually pleasant to query: one row per heat in
`Heats`, one row per stage/tilt event in `HeatEvents`, plus two views
(`vw_HeatAnalysis`, `vw_HeatDaily`) that pre-join and pivot them so an
analyst can point Power BI / Excel / SSMS at a single object.

Three things this module is careful about, because it sits behind a live
camera loop:

1. **It never blocks the caller.** `record_*()` just drops a small dict on
   a queue and returns. A single background thread owns the connection and
   does all the talking to SQL Server, so a slow or dead database can't
   stall frame processing or the state machine.

2. **It never loses a row.** If the database is unreachable the row is
   appended to a local spool file (`data/sql_spool.jsonl`) instead, and
   replayed *in order* the moment the connection comes back. The CSVs are
   still written either way, so there are two independent copies.

3. **It never double-writes.** Every event carries a client-generated
   `EventKey` and every insert is guarded by `WHERE NOT EXISTS`, so
   replaying a spool file (or re-running the CSV backfill) is safe.

If `pyodbc` isn't installed, or `SQL_ENABLED` is off, the whole thing
degrades to a no-op and the app runs exactly as it did before.
"""

import datetime
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid

import config

logger = logging.getLogger(__name__)

try:
    import pyodbc
except ImportError:  # pragma: no cover - depends on environment
    pyodbc = None


# Marker pushed onto the queue by stop() to wake the writer thread up.
_SENTINEL = object()

# Event keys are derived from (heat_id, ordinal, event_name) rather than
# being random, so the same event always produces the same key no matter
# how it reaches the database. That's what makes both the offline spool
# replay and migrate_csv_to_sql.py safe to run repeatedly - the
# WHERE NOT EXISTS guard on the insert simply no-ops the second time.
EVENT_NAMESPACE = uuid.UUID("6f1f4a58-3c7a-5b21-9a1e-7c2d0e5b4a90")


def make_event_key(heat_id, ordinal, event_name):
    return str(uuid.uuid5(EVENT_NAMESPACE, f"{heat_id}|{ordinal}|{event_name}"))


def parse_furnace_stand(furnace_id):
    """Split e.g. '4/2' or '4-2' into ('4', '2'). Defaults to (furnace_id, '')."""
    if not furnace_id:
        return ("", "")
    s = str(furnace_id)
    for sep in ("/", "-", "_"):
        if sep in s:
            parts = s.split(sep, 1)
            return (parts[0], parts[1])
    return (s, "")

# Preference order when config.SQL_DRIVER is not set explicitly.
_DRIVER_PREFERENCE = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
def detect_driver():
    """Return the best available SQL Server ODBC driver name, or None."""
    if config.SQL_DRIVER:
        return config.SQL_DRIVER
    if pyodbc is None:
        return None
    installed = set(pyodbc.drivers())
    for name in _DRIVER_PREFERENCE:
        if name in installed:
            return name
    # Last resort: anything that looks like a SQL Server driver.
    for name in sorted(installed):
        if "SQL Server" in name:
            return name
    return None


def build_connection_string(driver=None):
    driver = driver or detect_driver()
    if not driver:
        raise RuntimeError(
            "No SQL Server ODBC driver found. Install 'ODBC Driver 17 for SQL Server' "
            "(or 18) and try again."
        )
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={config.SQL_SERVER};"
        f"DATABASE={config.SQL_DATABASE};"
        f"UID={config.SQL_UID};"
        f"PWD={config.SQL_PASSWORD};"
        "TrustServerCertificate=yes;"
        # Driver 18 defaults Encrypt=yes; be explicit so 17 and 18 behave
        # the same way against a server without a trusted certificate.
        "Encrypt=yes;"
    )


def connect():
    """Open a single connection. Raises on failure - callers handle it."""
    if pyodbc is None:
        raise RuntimeError("pyodbc is not installed (pip install pyodbc)")
    conn = pyodbc.connect(build_connection_string(), timeout=config.SQL_LOGIN_TIMEOUT)
    conn.timeout = config.SQL_QUERY_TIMEOUT
    conn.autocommit = False
    return conn


def _q(identifier):
    """Bracket-quote an identifier, stripping anything that isn't safe."""
    cleaned = "".join(ch for ch in str(identifier) if ch.isalnum() or ch in "_$#")
    if not cleaned:
        raise ValueError(f"Unusable SQL identifier: {identifier!r}")
    return f"[{cleaned}]"


SCHEMA = _q(config.SQL_SCHEMA)
HEATS = f"{SCHEMA}.{_q(config.SQL_HEATS_TABLE)}"
EVENTS = f"{SCHEMA}.{_q(config.SQL_EVENTS_TABLE)}"
_HEATS_NAME = f"{config.SQL_SCHEMA}.{config.SQL_HEATS_TABLE}"
_EVENTS_NAME = f"{config.SQL_SCHEMA}.{config.SQL_EVENTS_TABLE}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Each entry is executed as its own batch: T-SQL requires CREATE VIEW to be
# alone in a batch, and it keeps a single failure from taking the rest down.
def schema_statements():
    return [
        # -- schema ------------------------------------------------------
        f"""
IF SCHEMA_ID('{config.SQL_SCHEMA}') IS NULL
    EXEC('CREATE SCHEMA {_q(config.SQL_SCHEMA)}');
""",
        # -- Heats: one row per heat ------------------------------------
        f"""
IF OBJECT_ID('{_HEATS_NAME}', 'U') IS NULL
CREATE TABLE {HEATS} (
    heat_id          VARCHAR(64)   NOT NULL CONSTRAINT PK_Heats PRIMARY KEY,
    furnace          VARCHAR(50)   NULL,
    stand            VARCHAR(50)   NULL,
    heat_number      INT           NULL,
    heat_status      VARCHAR(20)   NULL CONSTRAINT DF_Heats_heat_status DEFAULT ('IN_PROGRESS'),
    heat_start_time  DATETIME      NULL,
    heat_end_time    DATETIME      NULL,
    heat_duration    VARCHAR(50)   NULL,
    scrap_duration   VARCHAR(50)   NULL,
    sponge_duration  VARCHAR(50)   NULL,
    tapping_duration VARCHAR(50)   NULL,
    heat_event_match VARCHAR(5)    NULL,
    position         VARCHAR(20)   NULL,
    operation_status VARCHAR(20)   NULL,
    timeline_json    VARCHAR(MAX)  NULL,
    detected_classes VARCHAR(MAX)  NULL,
    insertion_time   DATETIME      NULL CONSTRAINT DF_Heats_insertion_time DEFAULT (SYSDATETIME()),
    updated_time     DATETIME      NULL CONSTRAINT DF_Heats_updated_time DEFAULT (SYSDATETIME()),
    FurnaceId        VARCHAR(16)   NOT NULL CONSTRAINT DF_Heats_FurnaceId DEFAULT ('')
);
""",
        # -- HeatEvents: one row per stage / tilt ------------------------
        f"""
IF OBJECT_ID('{_EVENTS_NAME}', 'U') IS NULL
CREATE TABLE {EVENTS} (
    id               INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_HeatEvents PRIMARY KEY,
    heat_id          VARCHAR(64)       NULL,
    event_id         VARCHAR(64)       NULL,
    cross_event_id   VARCHAR(64)       NULL,
    event_name       VARCHAR(100)      NULL,
    cross_event_name VARCHAR(100)      NULL,
    start_time       DATETIME          NULL,
    end_time         DATETIME          NULL,
    duration         VARCHAR(50)       NULL,
    status           VARCHAR(20)       NULL,
    confidence       VARCHAR(20)       NULL,
    sop_duration     VARCHAR(50)       NULL,
    alarms           VARCHAR(MAX)      NULL,
    original_frame   VARBINARY(MAX)    NULL,
    plotted_image    VARBINARY(MAX)    NULL,
    created_at       DATETIME          NULL CONSTRAINT DF_HeatEvents_created_at DEFAULT (SYSDATETIME()),
    updated_at       DATETIME          NULL CONSTRAINT DF_HeatEvents_updated_at DEFAULT (SYSDATETIME()),
    FurnaceId        VARCHAR(16)       NOT NULL CONSTRAINT DF_HeatEvents_FurnaceId DEFAULT ('')
);
""",
        # -- migration: ensure FurnaceId exists -------------------------
        f"""
IF OBJECT_ID('{_HEATS_NAME}', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.columns
                    WHERE object_id = OBJECT_ID('{_HEATS_NAME}') AND name = 'FurnaceId')
    ALTER TABLE {HEATS} ADD FurnaceId VARCHAR(16) NOT NULL CONSTRAINT DF_Heats_FurnaceId DEFAULT ('');
""",
        f"""
IF OBJECT_ID('{_EVENTS_NAME}', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.columns
                    WHERE object_id = OBJECT_ID('{_EVENTS_NAME}') AND name = 'FurnaceId')
    ALTER TABLE {EVENTS} ADD FurnaceId VARCHAR(16) NOT NULL CONSTRAINT DF_HeatEvents_FurnaceId DEFAULT ('');
""",
        # -- indexes -----------------------------------------------------
        f"""
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'UX_HeatEvents_event_id'
                 AND object_id = OBJECT_ID('{_EVENTS_NAME}'))
    CREATE UNIQUE INDEX UX_HeatEvents_event_id ON {EVENTS} (event_id) WHERE event_id IS NOT NULL;
""",
        f"""
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Heats_FurnaceId'
                 AND object_id = OBJECT_ID('{_HEATS_NAME}'))
    CREATE INDEX IX_Heats_FurnaceId ON {HEATS} (FurnaceId, heat_start_time);
""",
        f"""
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_FurnaceId'
                 AND object_id = OBJECT_ID('{_EVENTS_NAME}'))
    CREATE INDEX IX_HeatEvents_FurnaceId ON {EVENTS} (FurnaceId, heat_id);
""",
        f"""
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_heat_id'
                 AND object_id = OBJECT_ID('{_EVENTS_NAME}'))
    CREATE INDEX IX_HeatEvents_heat_id ON {EVENTS} (heat_id)
        INCLUDE (event_name, start_time, end_time, duration);
""",
        f"""
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_HeatEvents_name_start'
                 AND object_id = OBJECT_ID('{_EVENTS_NAME}'))
    CREATE INDEX IX_HeatEvents_name_start ON {EVENTS} (event_name, start_time);
""",
        f"""
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Heats_start_time'
                 AND object_id = OBJECT_ID('{_HEATS_NAME}'))
    CREATE INDEX IX_Heats_start_time ON {HEATS} (heat_start_time);
""",
        # -- vw_HeatAnalysis: 30 columns matching production -------------
        f"""
CREATE OR ALTER VIEW {SCHEMA}.[vw_HeatAnalysis] AS
WITH stages AS (
    SELECT
        heat_id,
        MIN(CASE WHEN event_name = 'scrap_charging'  THEN start_time END) AS ScrapStart,
        MAX(CASE WHEN event_name = 'scrap_charging'  THEN end_time   END) AS ScrapEnd,
        SUM(CASE WHEN event_name = 'scrap_charging'  THEN TRY_CAST(duration AS DECIMAL(10,2)) END) AS ScrapMinutes,
        MIN(CASE WHEN event_name = 'sponge_charging' THEN start_time END) AS SpongeStart,
        MAX(CASE WHEN event_name = 'sponge_charging' THEN end_time   END) AS SpongeEnd,
        SUM(CASE WHEN event_name = 'sponge_charging' THEN TRY_CAST(duration AS DECIMAL(10,2)) END) AS SpongeMinutes,
        MIN(CASE WHEN event_name = 'tapping'         THEN start_time END) AS TappingStart,
        MAX(CASE WHEN event_name = 'tapping'         THEN end_time   END) AS TappingEnd,
        SUM(CASE WHEN event_name = 'tapping'         THEN TRY_CAST(duration AS DECIMAL(10,2)) END) AS TappingMinutes,
        SUM(CASE WHEN event_name = 'tilt' THEN 1 ELSE 0 END)                                      AS TiltCount,
        SUM(CASE WHEN event_name = 'tilt' THEN TRY_CAST(duration AS DECIMAL(10,2)) ELSE 0 END)    AS TiltMinutes,
        SUM(CASE WHEN event_name = 'tilt_ignored_cooldown' THEN 1 ELSE 0 END)                     AS TiltsSuppressed,
        MAX(CASE WHEN event_name LIKE '%[_]unknown' THEN 1 ELSE 0 END)                            AS HasUnknownStage
    FROM {EVENTS}
    GROUP BY heat_id
)
SELECT
    h.heat_id                                     AS HeatId,
    h.FurnaceId                                   AS FurnaceId,
    h.heat_number                                 AS HeatNumber,
    ISNULL(h.heat_status, 'IN_PROGRESS')          AS Status,
    h.heat_start_time                             AS StartTime,
    h.heat_end_time                               AS EndTime,
    TRY_CAST(h.heat_duration AS DECIMAL(10,2))    AS HeatMinutes,
    CAST(h.heat_start_time AS DATE)               AS HeatDate,
    DATEPART(HOUR, h.heat_start_time)             AS HeatStartHour,
    DATENAME(WEEKDAY, h.heat_start_time)          AS HeatWeekday,
    s.ScrapStart, s.ScrapEnd,
    COALESCE(s.ScrapMinutes, TRY_CAST(h.scrap_duration AS DECIMAL(10,2)))       AS ScrapMinutes,
    s.SpongeStart, s.SpongeEnd,
    COALESCE(s.SpongeMinutes, TRY_CAST(h.sponge_duration AS DECIMAL(10,2)))     AS SpongeMinutes,
    s.TappingStart, s.TappingEnd,
    COALESCE(s.TappingMinutes, TRY_CAST(h.tapping_duration AS DECIMAL(10,2)))   AS TappingMinutes,
    ISNULL(s.TiltCount, 0)                        AS TiltCount,
    ISNULL(s.TiltMinutes, 0)                      AS TiltMinutes,
    ISNULL(s.TiltsSuppressed, 0)                  AS TiltsSuppressed,
    CASE WHEN TRY_CAST(h.heat_duration AS DECIMAL(10,2)) IS NOT NULL
         THEN TRY_CAST(h.heat_duration AS DECIMAL(10,2))
              - ISNULL(COALESCE(s.ScrapMinutes, TRY_CAST(h.scrap_duration AS DECIMAL(10,2))), 0)
              - ISNULL(COALESCE(s.SpongeMinutes, TRY_CAST(h.sponge_duration AS DECIMAL(10,2))), 0)
              - ISNULL(COALESCE(s.TappingMinutes, TRY_CAST(h.tapping_duration AS DECIMAL(10,2))), 0)
    END                                           AS UnaccountedMinutes,
    CASE WHEN COALESCE(s.ScrapMinutes, TRY_CAST(h.scrap_duration AS DECIMAL(10,2))) <= 45 THEN 1 ELSE 0 END AS ScrapWithinExpected,
    CASE WHEN COALESCE(s.SpongeMinutes, TRY_CAST(h.sponge_duration AS DECIMAL(10,2))) BETWEEN 90 AND 120 THEN 1 ELSE 0 END AS SpongeWithinExpected,
    CASE WHEN COALESCE(s.TappingMinutes, TRY_CAST(h.tapping_duration AS DECIMAL(10,2))) BETWEEN 10 AND 20 THEN 1 ELSE 0 END AS TappingWithinExpected,
    ISNULL(s.HasUnknownStage, 0)                  AS HasUnknownStage,
    CAST(NULL AS VARCHAR(255))                    AS CsvFile,
    CAST(NULL AS VARCHAR(128))                    AS SourceHost,
    ISNULL(h.updated_time, SYSDATETIME())         AS UpdatedAt
FROM {HEATS} h
LEFT JOIN stages s ON s.heat_id = h.heat_id;
""",
        # -- vw_HeatDaily: 12 columns ------------------------------------
        f"""
CREATE OR ALTER VIEW {SCHEMA}.[vw_HeatDaily] AS
SELECT
    FurnaceId,
    HeatDate,
    COUNT(*)                                  AS HeatsCompleted,
    CAST(AVG(HeatMinutes)    AS DECIMAL(10,2)) AS AvgHeatMinutes,
    CAST(MIN(HeatMinutes)    AS DECIMAL(10,2)) AS MinHeatMinutes,
    CAST(MAX(HeatMinutes)    AS DECIMAL(10,2)) AS MaxHeatMinutes,
    CAST(AVG(ScrapMinutes)   AS DECIMAL(10,2)) AS AvgScrapMinutes,
    CAST(AVG(SpongeMinutes)  AS DECIMAL(10,2)) AS AvgSpongeMinutes,
    CAST(AVG(TappingMinutes) AS DECIMAL(10,2)) AS AvgTappingMinutes,
    SUM(TiltCount)                            AS TotalTilts,
    CAST(AVG(CAST(TiltCount AS DECIMAL(10,2))) AS DECIMAL(10,2)) AS AvgTiltsPerHeat,
    SUM(TiltsSuppressed)                      AS TotalTiltsSuppressed
FROM {SCHEMA}.[vw_HeatAnalysis]
WHERE Status = 'COMPLETE' AND HasUnknownStage = 0
GROUP BY FurnaceId, HeatDate;
""",
    ]


# ---------------------------------------------------------------------------
# Parameterised statements
# ---------------------------------------------------------------------------
# UPDATE-then-INSERT rather than MERGE: simpler, and there is only ever one
# writer thread so the race MERGE would guard against can't happen here.
SQL_UPSERT_HEAT_START = f"""
UPDATE {HEATS}
   SET furnace = ?, stand = ?, FurnaceId = ?, heat_number = ?, heat_status = 'IN_PROGRESS',
       heat_start_time = ?, updated_time = SYSDATETIME()
 WHERE heat_id = ?;
IF @@ROWCOUNT = 0
INSERT INTO {HEATS} (heat_id, furnace, stand, FurnaceId, heat_number, heat_status,
                     heat_start_time, insertion_time, updated_time)
VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS', ?, SYSDATETIME(), SYSDATETIME());
"""

SQL_UPSERT_HEAT_END = f"""
UPDATE {HEATS}
   SET furnace = ?, stand = ?, FurnaceId = ?, heat_number = ?, heat_status = ?,
       heat_start_time = COALESCE(heat_start_time, ?), heat_end_time = ?,
       heat_duration = ?, scrap_duration = ?, sponge_duration = ?, tapping_duration = ?,
       updated_time = SYSDATETIME()
 WHERE heat_id = ?;
IF @@ROWCOUNT = 0
INSERT INTO {HEATS} (heat_id, furnace, stand, FurnaceId, heat_number, heat_status,
                     heat_start_time, heat_end_time, heat_duration, scrap_duration,
                     sponge_duration, tapping_duration, insertion_time, updated_time)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSDATETIME(), SYSDATETIME());
"""

SQL_UPSERT_EVENT = f"""
UPDATE {EVENTS}
   SET heat_id = ?, FurnaceId = ?, event_name = ?, start_time = ?, end_time = ?,
       duration = ?, status = ?, updated_at = SYSDATETIME()
 WHERE event_id = ?;
IF @@ROWCOUNT = 0
INSERT INTO {EVENTS} (event_id, heat_id, FurnaceId, event_name, start_time, end_time,
                      duration, status, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, SYSDATETIME(), SYSDATETIME());
"""


# ---------------------------------------------------------------------------
# (De)serialisation for the spool file
# ---------------------------------------------------------------------------
def _iso(value):
    return value.isoformat() if isinstance(value, datetime.datetime) else value


def _dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _str_duration(value):
    """Format duration into a string representation for VARCHAR(50), or None."""
    if value in (None, ""):
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------
class SqlRecorder:
    """Async, fire-and-forget writer. See module docstring."""

    def __init__(self, enabled=None, spool_file=None):
        self.enabled = config.SQL_ENABLED if enabled is None else enabled
        self.spool_file = spool_file or config.SQL_SPOOL_FILE
        self.host = socket.gethostname()[:128]

        self._queue = queue.Queue()
        self._thread = None
        self._conn = None
        self._schema_ready = False
        self._stopping = threading.Event()

        self._state_lock = threading.Lock()
        self._connected = False
        self._last_error = None
        self._last_write_at = None
        self._rows_written = 0
        self._rows_spooled = 0
        self._rows_replayed = 0
        self._next_retry_at = 0.0
        self._retry_delay = config.SQL_RETRY_MIN_SECONDS

        if self.enabled and pyodbc is None:
            self.enabled = False
            self._last_error = "pyodbc is not installed (pip install pyodbc)"
            logger.warning("SQL recording disabled: %s", self._last_error)

        if self.enabled and not config.SQL_PASSWORD:
            # Not fatal - the login may be passwordless or trusted - but
            # it's the most common reason for a first-run connect failure.
            logger.warning("SQL_PASSWORD is empty; set the SQL_PASSWORD "
                           "environment variable if the login needs one.")

        spool_parent = os.path.dirname(self.spool_file)
        if spool_parent:
            os.makedirs(spool_parent, exist_ok=True)

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------
    def start(self):
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="SqlRecorder", daemon=True)
        self._thread.start()
        logger.info("SQL recorder started -> %s / %s (driver: %s)",
                    config.SQL_SERVER, config.SQL_DATABASE, detect_driver() or "none found")

    def stop(self, timeout=10):
        """Flush what's queued, then close. Called on shutdown."""
        if self._thread is None:
            return
        self._stopping.set()
        self._queue.put(_SENTINEL)
        self._thread.join(timeout=timeout)
        self._thread = None

    # -----------------------------------------------------------------
    # Public recording API (called from the camera thread - must be fast)
    # -----------------------------------------------------------------
    def record_heat_started(self, heat_id, heat_number, start_time, csv_file=None,
                            furnace_id=None):
        self._enqueue({
            "op": "heat_start",
            "heat_id": heat_id,
            "furnace_id": furnace_id,
            "heat_number": heat_number,
            "start_time": _iso(start_time),
            "csv_file": csv_file,
        })

    def record_heat_completed(self, heat_id, heat_number, start_time, end_time,
                              duration_minutes, tilt_count=0, status="COMPLETE",
                              csv_file=None, furnace_id=None,
                              scrap_duration=None, sponge_duration=None,
                              tapping_duration=None):
        self._enqueue({
            "op": "heat_end",
            "heat_id": heat_id,
            "furnace_id": furnace_id,
            "heat_number": heat_number,
            "start_time": _iso(start_time),
            "end_time": _iso(end_time),
            "duration_minutes": duration_minutes,
            "scrap_duration": scrap_duration,
            "sponge_duration": sponge_duration,
            "tapping_duration": tapping_duration,
            "tilt_count": tilt_count,
            "status": status,
            "csv_file": csv_file,
        })

    def record_heat_deleted(self, heat_id, furnace_id=None):
        """Wipe a heat entirely: its Heats row and every HeatEvents row for
        it. Used by the active-fire watchdog (state_machine.py) when a heat
        has been running with no confirmed activefire for too long and is
        treated as invalid rather than a real heat."""
        self._enqueue({
            "op": "heat_delete",
            "heat_id": heat_id,
            "furnace_id": furnace_id,
        })

    def record_event(self, heat_id, heat_number, event_name, start_time, end_time,
                     duration_minutes=None, tilt_number=None, note=None,
                     ordinal=None, event_key=None, furnace_id=None):
        if event_key is None:
            event_key = (make_event_key(heat_id, ordinal, event_name)
                         if ordinal is not None else str(uuid.uuid4()))
        self._enqueue({
            "op": "event",
            "event_key": event_key,
            "heat_id": heat_id,
            "furnace_id": furnace_id,
            "heat_number": heat_number,
            "event_name": event_name,
            "start_time": _iso(start_time),
            "end_time": _iso(end_time),
            "duration_minutes": duration_minutes,
            "tilt_number": tilt_number,
            "note": note,
        })

    def status(self):
        with self._state_lock:
            slots = getattr(config, "FURNACE_SLOTS", {})
            return {
                "enabled": self.enabled,
                "connected": self._connected,
                "furnace_id": config.FURNACE_ID,
                "furnace_slots": {slug: cfg.get("furnace_id") for slug, cfg in slots.items()},
                "server": config.SQL_SERVER if self.enabled else None,
                "database": config.SQL_DATABASE if self.enabled else None,
                "driver": detect_driver() if self.enabled else None,
                "pending": self._queue.qsize(),
                "spooled": self._spool_line_count(),
                "rows_written": self._rows_written,
                "rows_spooled": self._rows_spooled,
                "rows_replayed": self._rows_replayed,
                "last_write_at": self._last_write_at,
                "last_error": self._last_error,
            }

    # -----------------------------------------------------------------
    # Writer thread
    # -----------------------------------------------------------------
    def _enqueue(self, payload):
        if not self.enabled:
            return
        payload["source_host"] = self.host
        payload.setdefault("furnace_id", config.FURNACE_ID)
        self._queue.put(payload)

    def _run(self):
        while True:
            try:
                item = self._queue.get(timeout=5)
            except queue.Empty:
                # Idle tick: if rows are waiting on disk from an earlier
                # outage, this is when we notice the database is back.
                if self._spool_line_count():
                    if self._ensure_connection():
                        self._replay_spool()
                continue

            if item is _SENTINEL:
                break
            self._handle(item)

        # Drain anything left over on shutdown so a clean stop doesn't
        # lose the last few rows.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _SENTINEL:
                self._handle(item)

        self._close_connection()

    def _handle(self, payload):
        if not self._ensure_connection():
            self._spool(payload)
            return
        # Anything spooled during an outage goes in first, so rows land in
        # the order they actually happened.
        self._replay_spool()
        if not self._execute(payload):
            self._spool(payload)

    def _execute(self, payload):
        """Run one payload against the open connection. False on failure."""
        try:
            cursor = self._conn.cursor()
            op = payload.get("op")

            if op == "heat_start":
                heat_id = payload["heat_id"]
                furnace_id = payload.get("furnace_id") or config.FURNACE_ID
                furnace, stand = parse_furnace_stand(furnace_id)
                number = _int(payload.get("heat_number"))
                start = _dt(payload.get("start_time"))
                cursor.execute(SQL_UPSERT_HEAT_START,
                               # UPDATE (6 params)
                               furnace, stand, furnace_id, number, start, heat_id,
                               # INSERT (6 params)
                               heat_id, furnace, stand, furnace_id, number, start)

            elif op == "heat_end":
                heat_id = payload["heat_id"]
                furnace_id = payload.get("furnace_id") or config.FURNACE_ID
                furnace, stand = parse_furnace_stand(furnace_id)
                number = _int(payload.get("heat_number"))
                start = _dt(payload.get("start_time"))
                end = _dt(payload.get("end_time"))
                duration_str = _str_duration(payload.get("duration_minutes"))
                scrap_dur_str = _str_duration(payload.get("scrap_duration"))
                sponge_dur_str = _str_duration(payload.get("sponge_duration"))
                tapping_dur_str = _str_duration(payload.get("tapping_duration"))
                status = payload.get("status") or "COMPLETE"
                cursor.execute(SQL_UPSERT_HEAT_END,
                               # UPDATE (12 params)
                               furnace, stand, furnace_id, number, status, start, end,
                               duration_str, scrap_dur_str, sponge_dur_str, tapping_dur_str, heat_id,
                               # INSERT (12 params)
                               heat_id, furnace, stand, furnace_id, number, status, start, end,
                               duration_str, scrap_dur_str, sponge_dur_str, tapping_dur_str)

            elif op == "event":
                key = payload["event_key"]
                heat_id = payload["heat_id"]
                furnace_id = payload.get("furnace_id") or config.FURNACE_ID
                event_name = payload["event_name"]
                start = _dt(payload.get("start_time"))
                end = _dt(payload.get("end_time"))
                duration_str = _str_duration(payload.get("duration_minutes"))
                status = payload.get("status") or ("COMPLETED" if end is not None else "IN_PROGRESS")
                
                cursor.execute(SQL_UPSERT_EVENT,
                               # UPDATE (8 params)
                               heat_id, furnace_id, event_name, start, end, duration_str, status, key,
                               # INSERT (8 params)
                               key, heat_id, furnace_id, event_name, start, end, duration_str, status)

            elif op == "heat_delete":
                heat_id = payload["heat_id"]
                # HeatEvents first: it has a logical (unenforced) FK to
                # Heats, and deleting the parent row first would just leave
                # its child rows orphaned instead of gone.
                cursor.execute(f"DELETE FROM {EVENTS} WHERE heat_id = ?", heat_id)
                cursor.execute(f"DELETE FROM {HEATS} WHERE heat_id = ?", heat_id)

            else:
                logger.warning("Ignoring unknown SQL payload op: %r", op)
                cursor.close()
                return True

            self._conn.commit()
            cursor.close()
            with self._state_lock:
                self._rows_written += 1
                self._last_write_at = datetime.datetime.now().isoformat(timespec="seconds")
                self._last_error = None
            return True

        except Exception as exc:  # pyodbc.Error and anything else
            logger.warning("SQL write failed (%s), spooling row: %s", payload.get("op"), exc)
            with self._state_lock:
                self._last_error = str(exc)[:500]
                self._connected = False
            self._close_connection()
            self._schedule_retry()
            return False

    # -----------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------
    def _ensure_connection(self):
        if self._conn is not None:
            return True
        if time.monotonic() < self._next_retry_at:
            return False
        try:
            self._conn = connect()
            if config.SQL_AUTO_CREATE_SCHEMA and not self._schema_ready:
                self._create_schema()
            with self._state_lock:
                self._connected = True
                self._last_error = None
            self._retry_delay = config.SQL_RETRY_MIN_SECONDS
            logger.info("Connected to SQL Server %s / %s",
                        config.SQL_SERVER, config.SQL_DATABASE)
            return True
        except Exception as exc:
            with self._state_lock:
                self._connected = False
                self._last_error = str(exc)[:500]
            self._close_connection()
            self._schedule_retry()
            logger.warning("SQL connect failed, retrying in %ss: %s", self._retry_delay, exc)
            return False

    def _schedule_retry(self):
        self._next_retry_at = time.monotonic() + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, config.SQL_RETRY_MAX_SECONDS)

    def _close_connection(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _create_schema(self):
        cursor = self._conn.cursor()
        for statement in schema_statements():
            try:
                cursor.execute(statement)
                self._conn.commit()
            except Exception as exc:
                # A failed view (e.g. SQL Server older than 2016 SP1, which
                # has no CREATE OR ALTER) shouldn't stop the tables from
                # being usable - the app only ever writes to the tables.
                self._conn.rollback()
                logger.warning("Schema statement failed (continuing): %s", exc)
        cursor.close()
        self._schema_ready = True
        logger.info("SQL schema verified: %s, %s (+ vw_HeatAnalysis, vw_HeatDaily)",
                    _HEATS_NAME, _EVENTS_NAME)

    # -----------------------------------------------------------------
    # Offline spool
    # -----------------------------------------------------------------
    def _spool(self, payload):
        try:
            with open(self.spool_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")
            with self._state_lock:
                self._rows_spooled += 1
        except OSError as exc:
            logger.error("Could not spool row to %s: %s", self.spool_file, exc)

    def _spool_line_count(self):
        try:
            with open(self.spool_file, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def _replay_spool(self):
        """Push every spooled row, oldest first, then clear the file. Rows
        that fail again are written back so nothing is dropped."""
        if not os.path.exists(self.spool_file):
            return
        try:
            with open(self.spool_file, "r", encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
        except OSError as exc:
            logger.error("Could not read spool file: %s", exc)
            return
        if not lines:
            return

        logger.info("Replaying %d spooled row(s) into SQL Server...", len(lines))
        remaining = []
        for i, line in enumerate(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Dropping unparseable spool line")
                continue
            if self._execute(payload):
                with self._state_lock:
                    self._rows_replayed += 1
            else:
                # Connection died mid-replay: keep this row and everything
                # after it, in order, for the next attempt.
                remaining = lines[i:]
                break

        try:
            if remaining:
                with open(self.spool_file, "w", encoding="utf-8") as f:
                    f.writelines(remaining)
            else:
                os.remove(self.spool_file)
                logger.info("Spool file drained and removed.")
        except OSError as exc:
            logger.error("Could not rewrite spool file: %s", exc)


# ---------------------------------------------------------------------------
# Read helpers (used by the API / verification, not by the hot path)
# ---------------------------------------------------------------------------
def fetch_recent_heats(limit=50, furnace_id=None):
    """Read the heat history back out of SQL Server. Raises on failure so
    callers can fall back to the CSV index.

    furnace_id: pass to restrict to one furnace (e.g. config.FURNACE_ID);
    leave as None to see every furnace sharing this database."""
    conn = connect()
    try:
        cursor = conn.cursor()
        if furnace_id:
            cursor.execute(f"""
                SELECT TOP (?)
                    HeatId, FurnaceId, HeatNumber, Status, StartTime, EndTime, HeatMinutes,
                    ScrapMinutes, SpongeMinutes, TappingMinutes, TiltCount,
                    TiltsSuppressed, HasUnknownStage
                FROM {SCHEMA}.[vw_HeatAnalysis]
                WHERE FurnaceId = ?
                ORDER BY StartTime DESC
            """, limit, furnace_id)
        else:
            cursor.execute(f"""
                SELECT TOP (?)
                    HeatId, FurnaceId, HeatNumber, Status, StartTime, EndTime, HeatMinutes,
                    ScrapMinutes, SpongeMinutes, TappingMinutes, TiltCount,
                    TiltsSuppressed, HasUnknownStage
                FROM {SCHEMA}.[vw_HeatAnalysis]
                ORDER BY StartTime DESC
            """, limit)
        columns = [c[0] for c in cursor.description]
        rows = []
        for row in cursor.fetchall():
            record = {}
            for name, value in zip(columns, row):
                if isinstance(value, datetime.datetime):
                    record[name] = value.isoformat()
                elif hasattr(value, "quantize"):  # Decimal
                    record[name] = float(value)
                else:
                    record[name] = value
            rows.append(record)
        cursor.close()
        return rows
    finally:
        conn.close()


def test_connection():
    """One-shot check used at startup / by check_sql.py. Returns a dict."""
    result = {"ok": False, "driver": detect_driver(), "error": None}
    if pyodbc is None:
        result["error"] = "pyodbc is not installed"
        return result
    try:
        conn = connect()
        cursor = conn.cursor()
        cursor.execute("SELECT @@VERSION, DB_NAME(), SUSER_SNAME()")
        version, database, login = cursor.fetchone()
        cursor.close()
        conn.close()
        result.update(ok=True, version=version.splitlines()[0] if version else None,
                      database=database, login=login)
    except Exception as exc:
        result["error"] = str(exc)
    return result