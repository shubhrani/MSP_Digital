"""
Central configuration for the Furnace Monitoring Dashboard.
Edit the values below to match your setup. Nothing else in the
codebase needs to change for a normal deployment.
"""

# ---------------------------------------------------------------------------
# Camera / stream
# ---------------------------------------------------------------------------
# Example: "rtsp://user:password@192.168.1.64:554/Streaming/Channels/101"
RTSP_URL = "rtsp://admin:Admin@1234@10.50.40.151:554/video/live?channel=1&subtype=0"

# If the RTSP connection drops, wait this many seconds before retrying.
RECONNECT_DELAY_SECONDS = 5

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
MODEL_PATH = "models/best_latest_model.pt"

# Minimum confidence for a detection to be considered at all.
CONFIDENCE_THRESHOLD = 0.3

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
# Data storage
# ---------------------------------------------------------------------------
DATA_DIR = "data/heats"                       # one CSV per heat lives here
INDEX_CSV = "data/heats_index.csv"            # summary row per heat

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
