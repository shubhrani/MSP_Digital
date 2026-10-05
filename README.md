# MSP Digital - Furnace Process Monitoring System

Industrial furnace process monitoring and steelmaking heat cycle tracking system deployed at MSP Steel & Power Ltd. Powered by computer vision (YOLO segmentation), real-time RTSP stream analysis, intelligent state machine logic, two-stage dynamic stream allocation, InfluxDB time-series metrics, and SQL Server persistence.

---

## 🏭 Overview & Architecture

The repository contains the complete production-grade deployment for both furnace complexes:

```
MSP_Digital/
├── Furnace4/                  # Production deployment for Furnace Complex 4 (Stands 4/1 & 4/2)
│   ├── app.py                 # Multi-furnace Flask application & streaming endpoints
│   ├── admin_app.py           # Administrative & data management interface
│   ├── camera_worker.py       # High-performance background RTSP frame capture & inference worker
│   ├── furnace_manager.py     # Multi-stream orchestrator, watchdog, & 2-stage stream swap logic
│   ├── detector.py            # Ultralytics YOLO segmentation wrapper & mask rendering
│   ├── state_machine.py       # Event-driven furnace cycle state machine with slot adoption
│   ├── debounce.py            # Temporal smoothing & flicker reduction filter
│   ├── config.py              # Configuration, 3-stream RTSP URLs, thresholds, probe timers
│   ├── db.py                  # SQL Server persistence layer (pyodbc)
│   ├── db_sync.py             # Resilient retry sync worker for offline tolerance
│   ├── temperature_monitor.py # InfluxDB temperature & power metric ingestion
│   ├── models/                # Trained YOLO segmentation models (.pt)
│   ├── static/                # Web dashboard assets (CSS, self-healing reconnect JS)
│   ├── templates/             # Real-time dashboard and administration templates
│   └── tests/                 # Unit test suite (state machine, furnace manager, stream swap)
│
├── Furnace5/                  # Production deployment for Furnace Complex 5 (Stands 5/1 & 5/2)
│   ├── app.py                 # Multi-furnace Flask application & streaming endpoints
│   ├── admin_app.py           # Administrative & data management interface
│   ├── camera_worker.py       # Background RTSP inference worker
│   ├── furnace_manager.py     # Stream pool manager, hot-spare promotion, slot transfer
│   ├── detector.py            # Ultralytics YOLO segmentation wrapper
│   ├── state_machine.py       # State machine logic for Furnace 5 cycle
│   ├── config.py              # Furnace 5 settings, RTSP streams, timeouts
│   ├── db.py                  # SQL Server persistence layer
│   ├── db_sync.py             # Data synchronization worker
│   ├── temperature_monitor.py # InfluxDB temperature monitoring
│   ├── purge_long_heats.py    # Database maintenance utility to purge orphan/anomalous heats
│   ├── run.sh                 # Auto-restart runner script with crash recovery
│   ├── models/                # Trained models for Furnace 5
│   ├── templates/             # Web UI templates
│   └── tests/                 # Unit test suite (furnace manager, state machine, db sync)
│
├── other data/                # Exploration tools, model checker, utility scripts
├── check_influx.py            # InfluxDB health & diagnostic utility
└── .gitignore                 # Git ignore definitions
```

---

## ⚡ Core Capabilities

### 1. Automated Heat Cycle Tracking
State machine tracks steelmaking milestones with millisecond precision:
- **Scrap Charging** (Coil down / charge bucket operation)
- **Sponge Iron Charging** (Continuous feeder / vibration detection)
- **Melting & Refining** (Active fire, thermal verification via InfluxDB)
- **Tapping** (Liquid steel discharge, ladle positioning, spark/stream signature)
- **Non-Tapping Furnace Tilts** (Deslagging, testing movements distinct from tapping)
- **Maintenance & Abort Handling** (Roof swung open, relining, maintenance classes)

### 2. Two-Stage Dynamic Stream Swapping & Slot Adoption
A 3-camera pool (`stream-1`, `stream-2`, `stream-3`) serves 2 active furnace stands:
- **Stage 1 (Maintenance Elimination):** Detects condition classes such as Class 28 (*Furnace Operation Under Maintenance*), Class 0 (*Coil Placed Near Furnace*), and Class 7 (*Preheating*). Streams exhibiting sustained maintenance conditions are excluded from active processing.
- **Stage 2 (Active-Fire Ranking & Hot-Spare Promotion):** Periodically probes spare streams with live-worker reuse. Warm active streams are assigned to the primary slots, while cold or idle streams are held as spares.
- **Seamless Slot Adoption (`adopt_state_machine`):** If a heat is in progress when streams swap between physical slots, the state machine, active heat CSV, state JSON, and index entries are dynamically transferred without losing telemetry or interrupting the ongoing heat cycle.

### 3. High-Resilience Video Pipeline
- **RTSP over TCP:** Hardened OpenCV capture options (`rtsp_transport;tcp|stimeout;5000000|max_delay;500000`) preventing packet-drop corruption and indefinite socket hangs.
- **Frontend Stream Self-Healing:** The dashboard monitors the MJPEG image stream and automatically re-establishes connection on socket disconnection or tab backgrounding/resumption.
- **Crash Recovery Daemon:** `run.sh` provides continuous process supervision and automated restarts for unattended operation.

### 4. Multi-Store Telemetry
- **Local CSV Journaling:** Per-heat CSV files recorded immediately on disk for offline safety.
- **Central SQL Server:** Stored in `Heats` and `HeatEvents` tables segregated by `FurnaceId`.
- **InfluxDB Integration:** High-frequency bath temperature and power ingestion synchronized against heat stages.
- **Audit Frame Snapshots:** Frame captures saved at each critical transition point for quality review.

---

## 🚀 Getting Started

### 1. Requirements & Dependencies
- Python 3.10+
- PyTorch & Ultralytics YOLO
- OpenCV (`opencv-python-headless`)
- Flask & Flask-CORS
- pyodbc & SQL Server ODBC Driver 17/18
- InfluxDB Client

Install dependencies:
```bash
pip install -r Furnace4/requirements.txt
```

### 2. Environment Configuration
Create `.env` inside `Furnace4` and `Furnace5` with the respective SQL Server credentials, InfluxDB tokens, and camera RTSP URLs:
```env
# SQL Server
DB_SERVER=your_sql_server
DB_DATABASE=FurnaceDB
DB_USER=your_user
DB_PASSWORD=your_password

# Camera RTSP Stream Pool
RTSP_STREAM_1=rtsp://user:pass@camera1_ip:554/stream1
RTSP_STREAM_2=rtsp://user:pass@camera2_ip:554/stream2
RTSP_STREAM_3=rtsp://user:pass@camera3_ip:554/stream3
```

### 3. Running the Services

**Furnace 4 Deployment:**
```bash
cd Furnace4
python app.py
```

**Furnace 5 Deployment:**
```bash
cd Furnace5
# Interactive mode:
python app.py

# Or background daemon mode with auto-restart:
bash run.sh
```

Dashboards are accessible at:
- **Furnace 4:** `http://localhost:8000/furnace/4-1` and `http://localhost:8000/furnace/4-2`
- **Furnace 5:** `http://localhost:8000/furnace/5-1` and `http://localhost:8000/furnace/5-2`

---

## 🧪 Testing

Execute the comprehensive test suites across both projects:

```bash
# Furnace 4 unit tests
python -m unittest discover -s Furnace4/tests

# Furnace 5 unit tests
python -m unittest discover -s Furnace5/tests
```

---

## 🛠️ Maintenance & Utility Scripts

- **`Furnace5/purge_long_heats.py`**: Deletes anomalous heats exceeding a duration threshold (default: >300 minutes). Supports `--apply`, `--furnace`, `--stand`, and logging audit flags.
- **`Furnace5/run.sh`**: Auto-restart loop ensuring continuous service operation.
- **`check_influx.py`**: InfluxDB connection and bucket health verification script.
