"""
Central configuration for the Furnace Monitoring Dashboard.
Edit the values below to match your setup. Nothing else in the
codebase needs to change for a normal deployment.
"""

import os

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Camera / stream
# ---------------------------------------------------------------------------
# Example: "rtsp://user:password@192.168.1.64:554/Streaming/Channels/101"
# Kept for backward compatibility with anything that still imports
# config.RTSP_URL directly (tests, one-off scripts). The running app itself
# reads the per-furnace URL out of FURNACES below.
RTSP_URL = "rtsp://admin:Admin@1234@10.50.40.152:554/video/live?channel=1&subtype=0"

# If the RTSP connection drops, wait this many seconds before retrying.
RECONNECT_DELAY_SECONDS = 5

# ---------------------------------------------------------------------------
# Model
MODEL_PATH = os.getenv(
    "MODEL_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "furnace_monitor_best.pt"),
)

# Minimum confidence for a detection to be considered at all.
CONFIDENCE_THRESHOLD = 0.5

# Run detection on every Nth frame (1 = every frame). Raise this on slower
# hardware / CPU-only machines to keep the video feed smooth. The video
# stream itself is never skipped, only how often YOLO actually runs.
PROCESS_EVERY_N_FRAMES = 1

# A class must be detected on this many *consecutive processed* frames
# before it is treated as "confirmed" and passed to the process logic.
# This filters out single-frame flicker / misdetections. Lower this if
# state changes feel sluggish, raise it if you see false triggers.
DEBOUNCE_FRAMES = 5

# JPEG quality used for the MJPEG stream served to the browser (1-100).
JPEG_QUALITY = 80

# ---------------------------------------------------------------------------
# Furnaces
# ---------------------------------------------------------------------------
# This one Flask app now watches BOTH furnace cameras at once, each on its
# own dashboard page (/furnace/4-1 and /furnace/4-2), each with its own
# CameraWorker + HeatStateMachine + CSV files. Both push into the SAME
# SQL Server database/tables (Heats, HeatEvents); every row is tagged with
# FurnaceId so the two furnaces stay segregated there too
# (WHERE FurnaceId = '4/1' vs '4/2').
#
# Each entry:
#   furnace_id     - value stored in the SQL FurnaceId column / shown in UI
#   name            - display name on the dashboard
#   rtsp_url        - that furnace's camera stream
#   data_dir        - where that furnace's per-heat CSVs live
#   index_csv       - that furnace's heat-history summary CSV
#   heat_id_prefix  - prefix used when generating heat IDs (e.g. "Heat_003_...").
#                      Two furnaces must never generate the same HeatId, since
#                      it's the primary key in the shared Heats table - hence
#                      the different prefix per furnace. IMPORTANT: keep this
#                      prefix free of underscores (it's split on "_" to parse
#                      the heat number back out).
#
# 4/2 keeps the exact same data_dir/index_csv/prefix ("data/heats",
# "data/heats_index.csv", "Heat") it already uses today, so this update
# doesn't touch or renumber the heat history already on disk / in SQL for
# that furnace. 4/1 is a fresh deployment with its own files.
#
# Every value can be overridden by an environment variable, so the same zip
# can be copied between machines without editing this file - e.g. if a
# camera's IP or credentials change:
#     export FURNACE_4_1_RTSP="rtsp://user:pass@10.50.40.151:554/..."
#     export FURNACE_4_2_RTSP="rtsp://user:pass@10.50.40.152:554/..."
# ---------------------------------------------------------------------------
# Physical camera streams (3 of them) — camera only, no PYRO here.
# ---------------------------------------------------------------------------
STREAMS = [
    {
        "stream_id": "stream-1",
        "name": "Camera 10.50.40.153",
        "rtsp_url": os.getenv(
            "STREAM_1_RTSP",
            "rtsp://admin:Admin@1234@10.50.40.153:554/video/live?channel=1&subtype=0",
        ),
    },
    {
        "stream_id": "stream-2",
        "name": "Camera 10.50.40.152",
        "rtsp_url": os.getenv(
            "STREAM_2_RTSP",
            "rtsp://admin:Admin@1234@10.50.40.152:554/video/live?channel=1&subtype=0",
        ),
    },
    {
        "stream_id": "stream-3",
        "name": "Camera 10.50.40.151",
        "rtsp_url": os.getenv(
            "STREAM_3_RTSP",
            "rtsp://admin:Admin@1234@10.50.40.151:554/video/live?channel=1&subtype=0",
        ),
    },
]

# ---------------------------------------------------------------------------
# Logical furnace slots — these carry PYRO, because PYRO is a property
# of the furnace identity, not of whatever camera happens to be backing it.
#
# PYRO4/1 follows whichever camera is currently acting as 4/1.
# PYRO4/2 follows whichever camera is currently acting as 4/2.
# Both tags are fixed for the lifetime of the process; only the camera
# behind each slot changes during a swap.
# ---------------------------------------------------------------------------
FURNACE_SLOTS = {
    "4-1": {
        "furnace_id": "4/1",
        "name": "Furnace 4/1",
        "data_dir": os.getenv("FURNACE_4_1_DATA_DIR", "data/heats_4_1"),
        "index_csv": os.getenv("FURNACE_4_1_INDEX_CSV", "data/heats_index_4_1.csv"),
        "heat_id_prefix": "Heat41",
        "influx_group": os.getenv("FURNACE_4_1_INFLUX_GROUP", "SFC4/1"),
        "pyro_field":   os.getenv("FURNACE_4_1_PYRO_FIELD",   "PYRO4/1"),
    },
    "4-2": {
        "furnace_id": "4/2",
        "name": "Furnace 4/2",
        # Kept identical to today's paths so existing history isn't touched.
        "data_dir": os.getenv("FURNACE_4_2_DATA_DIR", "data/heats"),
        "index_csv": os.getenv("FURNACE_4_2_INDEX_CSV", "data/heats_index.csv"),
        "heat_id_prefix": "Heat",
        "influx_group": os.getenv("FURNACE_4_2_INFLUX_GROUP", "SFC4/2"),
        "pyro_field":   os.getenv("FURNACE_4_2_PYRO_FIELD",   "PYRO4/2"),
    },
}

# How long to probe each stream at startup to decide if class 28 is present.
STREAM_PROBE_SECONDS = float(os.getenv("STREAM_PROBE_SECONDS", "6"))

# How long an assigned stream must go without class 3 before the manager
# treats the furnace behind it as cold and re-evaluates every stream.
ACTIVEFIRE_TIMEOUT_MINUTES = float(
    os.getenv("ACTIVEFIRE_TIMEOUT_MINUTES", "5")
)

# How often the manager's watchdog checks each assigned slot. 30 s means a
# cold stream is noticed within 30 s of crossing the 5-minute mark, so the
# worst case from "furnace goes cold" to "re-evaluation starts" is 5m30s.
SLOT_POLL_SECONDS = float(os.getenv("SLOT_POLL_SECONDS", "30"))

# Duration of each stream's active-fire probe. Probes run in PARALLEL, so
# total wall-clock cost is this value, not 3× this value.
PROBE_ACTIVEFIRE_SECONDS = float(os.getenv("PROBE_ACTIVEFIRE_SECONDS", "8"))

# Backward compatibility alias
STREAM_ACTIVEFIRE_TIMEOUT_MINUTES = ACTIVEFIRE_TIMEOUT_MINUTES

# Which furnace page "/" redirects to.
DEFAULT_FURNACE_SLUG = "4-2"

# Kept for backward compatibility with scripts (check_sql.py, migrate_csv_to_sql.py
# when run without --furnace-id, tests) that still assume a single default.
FURNACE_ID = os.getenv("FURNACE_ID", FURNACE_SLOTS["4-2"]["furnace_id"])

# Backward compatibility alias for legacy scripts expecting config.FURNACES
FURNACES = FURNACE_SLOTS

# ---------------------------------------------------------------------------
# InfluxDB (Temperature / PYRO tracking)
# ---------------------------------------------------------------------------
INFLUX_ENABLED = os.getenv("INFLUX_ENABLED", "1").lower() not in ("0", "false", "no")
INFLUX_URL = os.getenv("INFLUX_URL", "http://13.127.217.106:8086")

# Preferred: API token (InfluxDB 2.x -> Load Data -> API Tokens -> copy).
# Use read access on the bucket. Set with e.g.
#     export INFLUX_TOKEN="your-long-token-here"
# On Windows:  setx INFLUX_TOKEN "your-long-token-here"
# Leave empty only if the bucket is genuinely world-readable.
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")

# Alternative when using username/password (older setups):
#     export INFLUX_USERNAME="admin"
#     export INFLUX_PASSWORD="..."
INFLUX_USERNAME = os.getenv("INFLUX_USERNAME", "")
INFLUX_PASSWORD = os.getenv("INFLUX_PASSWORD", "")

# Confirm these are exactly what the token was issued for:
#   org id   (InfluxDB 2.x -> Settings -> Organization ID)
#   bucket   (InfluxDB 2.x -> Load Data -> Buckets -> name)
INFLUX_ORG_ID = os.getenv("INFLUX_ORG_ID", "065d464f9c4c6b4f")
INFLUX_ORG = os.getenv("INFLUX_ORG", INFLUX_ORG_ID)
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "MSPRAIGARH")
INFLUX_MEASUREMENT = os.getenv("INFLUX_MEASUREMENT", "SMS")
INFLUX_UNIT = os.getenv("INFLUX_UNIT", "SMS_Furnace")
INFLUX_POLL_SECONDS = float(os.getenv("INFLUX_POLL_SECONDS", "1.0"))
INFLUX_MAX_AGE_SECONDS = float(os.getenv("INFLUX_MAX_AGE_SECONDS", "5.0"))

# When a furnace movement class becomes confirmed, look slightly backward
# through recent PYRO samples. This handles YOLO/debounce delay where the
# temperature may already have changed before class 2/8/18/24/25 is confirmed.
INFLUX_MOVEMENT_LOOKBACK_SECONDS = float(
    os.getenv("INFLUX_MOVEMENT_LOOKBACK_SECONDS", "3.0")
)

# ---------------------------------------------------------------------------
# Data storage
# ---------------------------------------------------------------------------
# Legacy single-furnace defaults - still used by anything that imports these
# directly (tests, migrate_csv_to_sql.py's argparse defaults). The running
# app itself uses each furnace's own data_dir/index_csv from FURNACES above.
DATA_DIR = FURNACE_SLOTS["4-2"]["data_dir"]        # one CSV per heat lives here
INDEX_CSV = FURNACE_SLOTS["4-2"]["index_csv"]      # summary row per heat

# ---------------------------------------------------------------------------
# Debug frames
# ---------------------------------------------------------------------------
# Every stage start/end queues a "capture this frame" request. CameraWorker
# writes the raw frame + a small .json sidecar under here, one folder per
# heat_id, and the dashboard shows them so a transition can be checked
# against the actual video.
FRAMES_DIR = os.getenv("FRAMES_DIR", "data/frames")

# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------
# Every value can be overridden by an environment variable of the same name,
# so the server/credentials never have to be edited into a file that gets
# committed or copied between machines. On the plant PC:
#
#     export SQL_SERVER="13.200.63.36,1433"
#     export SQL_PASSWORD="..."       # or setx on Windows
#
# Turn the whole SQL path off (CSV only, exactly like before) with
# SQL_ENABLED=0 - useful when testing away from the plant network.
SQL_ENABLED = os.getenv("SQL_ENABLED", "1").lower() not in ("0", "false", "no")

SQL_SERVER = os.getenv("SQL_SERVER", "10.135.20.195,1433")
SQL_DATABASE = os.getenv("SQL_DATABASE", "Furnace")
SQL_UID = os.getenv("SQL_UID", "sa")
SQL_PASSWORD = os.getenv("SQL_PASSWORD", "Mampa#80631")

# Leave empty to auto-detect the best installed ODBC driver
# (18 -> 17 -> 13 -> Native Client -> "SQL Server").
SQL_DRIVER = os.getenv("SQL_DRIVER", "ODBC Driver 17 for SQL Server")

SQL_SCHEMA = os.getenv("SQL_SCHEMA", "dbo")
SQL_HEATS_TABLE = "Heats"
SQL_EVENTS_TABLE = "HeatEvents"

# Create the tables/indexes/views on first connect if they aren't there.
SQL_AUTO_CREATE_SCHEMA = True

# Seconds. Login timeout is deliberately short: a dead database should be
# noticed and spooled around quickly, not sat on.
SQL_LOGIN_TIMEOUT = 5
SQL_QUERY_TIMEOUT = 30

# Reconnect backoff after a failure: starts at MIN, doubles up to MAX.
SQL_RETRY_MIN_SECONDS = 5
SQL_RETRY_MAX_SECONDS = 300

# Rows that couldn't be written are appended here and replayed in order
# once the database comes back. Deleted automatically once drained.
SQL_SPOOL_FILE = "data/sql_spool.jsonl"

# On startup, push any heat CSVs that aren't in the database yet - in a
# background thread, so it never delays the camera or the dashboard.
# Idempotent: re-running it inserts nothing new.
SQL_BACKFILL_ON_START = True

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
FLASK_HOST = "0.0.0.0"
FLASK_PORT = 8000
FLASK_DEBUG = False

# ---------------------------------------------------------------------------
# Class map (as provided). Used only for drawing labels if the weights file
# does not carry its own names. The state-machine logic matches on the
# numeric class IDs below, not on these strings, so it keeps working even
# if your model's label text differs slightly.
# ---------------------------------------------------------------------------
CLASS_NAMES = {
    0: "negative_sample_pillers",
    1: "sponge_feeder_farfromfurnace",
    2: "furnace_position_up",
    3: "furnace_operation_activefire",
    4: "negative_sample_scraponground",
    5: "negative_sample_otherfurnaces",
    6: "sponge_feeder_engaged",
    7: "negative_sample_cranebeam",
    8: "furnace_bottom_visiblepartially",
    9: "furnace_position_normal",
    10: "sponge_feeder_onfurnace",
    11: "negative_sample_fan",
    12: "negative_sample_furnacesmoke",
    13: "laddle_moving_empty",
    14: "negative_sample_sunlight",
    15: "magnet_moving_idle",
    16: "magnet_moving_withscrap",
    17: "magnet_feeding_scrap",
    18: "furnace_position_tilting",
    19: "furnace_operation_scrapfeeding",
    20: "negative_sample_slag",
    21: "human_carring_silicomanganese",
    22: "human_feeding_silicomanganese",
    23: "negative_sample_notvisible",
    24: "furnace_bottom_visiblecompletely",
    25: "laddle_moving_withmeltiron",
    26: "sponge_feeder_notengaged",
    27: "magnet_moving_withshaker",
    28: "furnace_operation_undermaintenance",
    29: "coil_position_placednearfurnace",
    30: "negitive_sample_rammingmusscontainer",
    31: "negative_sample_magnetonground",
    32: "furma_position_placednearfurnace",
    33: "furnace_operation_furmareplaced",
    34: "negative_sample_hanggingcreate",
    35: "magnet_moving_withsponge",
    36: "magnet_moving_slag",
    37: "magnet_feeding_sponge",
}
