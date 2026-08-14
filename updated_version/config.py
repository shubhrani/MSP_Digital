"""
Central configuration for the Furnace Monitoring Dashboard.
Edit the values below to match your setup. Nothing else in the
codebase needs to change for a normal deployment.
"""

import os

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
# ---------------------------------------------------------------------------
MODEL_PATH = "models/best_latest_model.pt"

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
FURNACES = {
    "4-1": {
        "furnace_id": "4/1",
        "name": "Furnace 4/1",
        "rtsp_url": os.getenv(
            "FURNACE_4_1_RTSP",
            "rtsp://admin:Admin@1234@10.50.40.151:554/video/live?channel=1&subtype=0",
        ),
        "data_dir": os.getenv("FURNACE_4_1_DATA_DIR", "data/heats_4_1"),
        "index_csv": os.getenv("FURNACE_4_1_INDEX_CSV", "data/heats_index_4_1.csv"),
        "heat_id_prefix": "Heat41",
    },
    "4-2": {
        "furnace_id": "4/2",
        "name": "Furnace 4/2",
        "rtsp_url": os.getenv("FURNACE_4_2_RTSP", "rtsp://admin:Admin@1234@10.50.40.152:554/video/live?channel=1&subtype=0"),
        "data_dir": os.getenv("FURNACE_4_2_DATA_DIR", "data/heats"),
        "index_csv": os.getenv("FURNACE_4_2_INDEX_CSV", "data/heats_index.csv"),
        "heat_id_prefix": "Heat",
    },
}

# Which furnace page "/" redirects to.
DEFAULT_FURNACE_SLUG = "4-2"

# Kept for backward compatibility with scripts (check_sql.py, migrate_csv_to_sql.py
# when run without --furnace-id, tests) that only ever cared about one furnace.
FURNACE_ID = os.getenv("FURNACE_ID", FURNACES["4-2"]["furnace_id"])

# ---------------------------------------------------------------------------
# Data storage
# ---------------------------------------------------------------------------
# Legacy single-furnace defaults - still used by anything that imports these
# directly (tests, migrate_csv_to_sql.py's argparse defaults). The running
# app itself uses each furnace's own data_dir/index_csv from FURNACES above.
DATA_DIR = FURNACES["4-2"]["data_dir"]        # one CSV per heat lives here
INDEX_CSV = FURNACES["4-2"]["index_csv"]      # summary row per heat

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
    0: "negitive_sample_notvisible",
    1: "spange_feeder_farfromfurnace",
    2: "furnace_position_normal",
    3: "magnet_feeding_scrap",
    4: "magnet_moving_ideal",
    5: "negitive_sample_sunlight",
    6: "negitive_sample_fan",
    7: "negitive_sample_otherfurnaces",
    8: "spange_feeder_onfurnace",
    9: "furnace_operation_activefire",
    10: "negetive_sample_craenbeem",
    11: "negative_sample_scraponground",
    12: "furnace_position_tilding",
    13: "furnace_bottom_visiblepartially",
    14: "negitive_sample_pillers",
    15: "negative_sample_furnacesmoke",
    16: "furnace_bottom_visiblecompletely",
    17: "magnet_moving_withscrap",
    18: "furnace_position_up",
    19: "laddle_moving_empty",
    20: "negative_sample_slab",
    21: "scrap_mettal_onground",
    22: "negetive_sample_crainbeem",
    23: "spange_feeder_engaged",
    24: "spange_feeder_notengaged",
    25: "magnet_moving_slab",
    26: "laddle_moving_withmeltiron",
}
