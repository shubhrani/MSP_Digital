# MSP Digital - Furnace Process Monitoring System

Industrial furnace process monitoring and steelmaking heat cycle tracking system deployed at MSP Steel & Power Ltd. Powered by computer vision (YOLO segmentation), real-time RTSP stream analysis, intelligent state machine logic, InfluxDB time-series metrics, and SQL Server persistence.

---

## 🏭 Overview & Architecture

The repository contains the complete production-grade deployment for the furnace complexes:

```
MSP_Digital/
├── Furnace4/            # Full production deployment for Furnaces 4/1 & 4/2
│   ├── app.py           # Multi-furnace Flask application & streaming endpoints
│   ├── admin_app.py     # Administrative & data management interface
│   ├── camera_worker.py # High-performance background RTSP frame capture & inference worker
│   ├── detector.py      # Ultralytics YOLO segmentation wrapper & mask rendering
│   ├── state_machine.py # Event-driven furnace cycle state machine
│   ├── debounce.py      # Temporal smoothing & flicker reduction filter
│   ├── config.py        # System configuration, RTSP URLs, thresholds, intervals
│   ├── db.py            # SQL Server persistence layer
│   ├── db_sync.py       # Resilient retry sync worker for offline tolerance
│   ├── temperature_monitor.py # InfluxDB temperature & power metric ingestion
│   ├── models/          # Trained YOLO segmentation models (.pt)
│   ├── static/          # Web dashboard assets (CSS, JS)
│   ├── templates/       # Real-time dashboard and administration templates
│   └── tests/           # Unit and integration test suites
│
├── Furnace5/            # Full production deployment for Furnaces 5/1 & 5/2
│   ├── app.py           # Multi-furnace Flask application & streaming endpoints
│   ├── admin_app.py     # Administrative & data management interface
│   ├── camera_worker.py # Background RTSP inference worker
│   ├── detector.py      # Ultralytics YOLO segmentation wrapper
│   ├── state_machine.py # State machine logic for Furnace 5 cycle
│   ├── config.py        # Furnace 5 specific settings & endpoints
│   ├── db.py            # SQL Server persistence layer
│   ├── db_sync.py       # Data synchronization worker
│   ├── temperature_monitor.py # InfluxDB temperature monitoring
│   ├── models/          # Trained models for Furnace 5
│   └── templates/       # Web UI templates
│
├── other data/          # Reference training scripts, exploration tools, utility code
├── check_influx.py      # InfluxDB health & diagnostic utility
└── .gitignore           # Git ignore definitions
```

---

## ⚡ Core Capabilities

1. **Automated Heat Cycle Tracking**: Automatically identifies cycle stages:
   - Scrap Charging
   - Sponge Charging
   - Melting / Refining
   - Tapping
   - Furnace Tilts & Non-tapping movements
   - Furnace Maintenance / Heat Abort conditions
2. **Dual-Furnace Real-Time Dashboard**: Single-process architecture simultaneously managing dual RTSP feeds per unit with low latency.
3. **Multi-Store Telemetry**:
   - Local CSV backups per heat
   - Central SQL Server database for business intelligence and shift reporting
   - InfluxDB integration for high-frequency temperature tracking
4. **Visual Event Capture**: Saves event frame snapshots on critical state transitions for historical audit.

---

## 🚀 Getting Started

### 1. Requirements & Dependencies
- Python 3.10+
- PyTorch & Ultralytics YOLO
- OpenCV (`opencv-python-headless`)
- Flask & Flask-CORS
- InfluxDB Client & ODBC Drivers for SQL Server

Install dependencies:
```bash
pip install -r Furnace4/requirements.txt
```

### 2. Environment Configuration
Create `.env` inside `Furnace4` and `Furnace5` with respective database connection strings and RTSP URLs:
```env
DB_SERVER=your_sql_server
DB_DATABASE=FurnaceDB
DB_USER=your_user
DB_PASSWORD=your_password
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
python app.py
```

Dashboards are accessible at:
- `http://localhost:8000/furnace/4-1` and `http://localhost:8000/furnace/4-2`
- `http://localhost:8000/furnace/5-1` and `http://localhost:8000/furnace/5-2`
