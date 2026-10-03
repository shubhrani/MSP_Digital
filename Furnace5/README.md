# Furnace Process Monitor

> **Now watching two furnaces.** This app runs both 4/1 and 4/2 out of one
> Flask process, each with its own camera, own dashboard page, and own CSV
> files, all pushed into the same SQL Server database segregated by
> `FurnaceId`. See `config.py`'s `FURNACES` dict to change either camera's
> RTSP URL, and jump to **"Two furnaces, one app"** below for the details.

A Flask dashboard that watches an RTSP camera feed with a YOLOv11
*segmentation* model (`best.pt`), figures out which stage of the
steelmaking heat cycle the furnace is in (scrap charging &rarr; sponge
charging &rarr; tapping), counts furnace tilts that are *not* tapping, and
logs the start/end time of every stage to a CSV file per heat.

```
furnace_monitor/
├── app.py              Flask app: routes for BOTH furnaces, video streams, JSON APIs
├── camera_worker.py    Background thread: RTSP read + YOLO + drawing (one per furnace)
├── detector.py         Thin YOLO wrapper + segmentation-mask drawing helpers
├── state_machine.py    The actual process logic (pure Python, unit tested)
├── debounce.py         Filters single-frame detection flicker
├── config.py           All the settings you're likely to want to change,
│                        including FURNACES (per-furnace camera/CSV config)
├── templates/dashboard.html   one furnace's page (rendered per-furnace)
├── templates/home.html        furnace picker (only used as a fallback)
├── static/css/style.css
├── static/js/dashboard.js
├── models/              <- put best.pt here (shared by both furnaces)
├── data/heats/                4/2's per-heat CSVs (existing, unchanged)
├── data/heats_index.csv       4/2's heat summary CSV (existing, unchanged)
├── data/heats_4_1/            4/1's per-heat CSVs (new)
├── data/heats_index_4_1.csv   4/1's heat summary CSV (new)
└── tests/test_state_machine.py   run with `python3 tests/test_state_machine.py`
```

## 0. Two furnaces, one app

`config.py` has a `FURNACES` dict with one entry per furnace - each one's
`rtsp_url`, `data_dir`, `index_csv`, and `heat_id_prefix`. 4/2 keeps the
exact paths it already used (`data/heats`, `data/heats_index.csv`), so
upgrading to this version doesn't touch or renumber its existing history.
4/1 is new and gets its own files.

Once running (`python3 app.py`), each furnace has its own page:

```
http://<machine-ip>:8000/furnace/4-1
http://<machine-ip>:8000/furnace/4-2
```

`http://<machine-ip>:8000/` redirects to the default furnace
(`config.DEFAULT_FURNACE_SLUG`); a small switcher in the top bar of each
dashboard flips between the two. Both furnaces write into the *same* SQL
Server `Heats`/`HeatEvents` tables - every row carries a `FurnaceId`
column (`4/1` or `4/2`) so a query like
`SELECT * FROM Heats WHERE FurnaceId = '4/1'` (see
`sql/analysis_queries.sql`) gets just that furnace's data back.

To point either camera at a different URL without editing `config.py`,
set the matching environment variable before `python3 app.py`:

```bash
export FURNACE_4_1_RTSP="rtsp://user:pass@10.50.40.151:554/video/live?channel=1&subtype=0"
export FURNACE_4_2_RTSP="rtsp://user:pass@10.50.40.152:554/video/live?channel=1&subtype=0"
```

Both furnaces currently load their own copy of the YOLO model (one per
camera thread) rather than sharing a single loaded model, which is simpler
and avoids any question of thread-safety in concurrent `model.predict()`
calls - the tradeoff is the model's memory footprint is paid twice. If
that becomes a problem on the deployment machine, that's the place to
optimize next.

To backfill one furnace's CSV history into SQL Server by hand (the app
already does this automatically in the background on startup):

```bash
python3 migrate_csv_to_sql.py --furnace-id 4/1
python3 migrate_csv_to_sql.py --furnace-id 4/2 --dry-run
```


## 1. Setup

```bash
cd furnace_monitor
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Then:

1. Copy your trained weights into `models/best.pt` (or point `MODEL_PATH`
   in `config.py` at wherever it actually lives).
2. Open `config.py` and set `RTSP_URL` to your camera's stream URL.
3. Run it:

```bash
python3 app.py
```

4. Open `http://<machine-ip>:5000` in a browser. The video feed, current
   heat/stage, tilt count and heat history table are all on that one page.

`ultralytics` will pull in PyTorch, so the first `pip install` can take a
few minutes and a couple of GB of disk. If you have an NVIDIA GPU and CUDA
set up, `ultralytics` will use it automatically for faster inference.

## 2. Model & video streaming

**Model:** `models/best.pt` is expected to be a **YOLOv11 segmentation**
model (trained from a `yolo11*-seg.pt` base), not a plain bounding-box
detector. It loads the same way either way (`ultralytics.YOLO(path)`), but
`detector.py` now also reads `results.masks` alongside `results.boxes`, so
the video overlay draws the actual segmented outline (filled + traced) for
each detection instead of just a rectangle. If a particular detection has
no mask for some reason, it quietly falls back to drawing its box, so this
still works against older non-seg weights too. `requirements.txt` pins
`ultralytics>=8.3`, which is the version that added YOLO11 support.

**Video streaming:** `/video_feed` is served straight from a generator.
`CameraWorker.frames()` is a `yield`-based generator that wakes up via a
`threading.Condition` the instant the capture loop publishes a new
annotated JPEG, and `app.py`'s `_mjpeg_generator()` just does
`for jpeg in camera_worker.frames(): yield ...` &mdash; no polling loop, no
fixed `time.sleep()`. It's still capped at `MAX_STREAM_FPS` (20, in
`camera_worker.py`) so a fast source can't flood a slow client, but frames
above that rate are skipped rather than queued, and duplicate frames (no
new frame published since the last one sent) are never resent.

## 3. What triggers what (43-Class Model)

The process logic matches the **numeric class IDs** from the 43-class YOLO model. The production sequence is strictly maintained inside one heat:

**Scrap charging (24, 21, or 37) -> Sponge charging (15, 4, 6, 19, 27, or 2) -> Tapping (18 + Ladle 25, or 36) -> Normal (1) -> COMPLETE**

| ID | Class | Effect |
|----|-------|--------|
| 24 | `magnet_feeding_scrap` | Starts a new heat and starts scrap charging. Primary scrap charging trigger. |
| 21 | `furnace_operation_scrapfeeding` | Starts a new heat and starts scrap charging. Co-trigger with #24. |
| 37 | `human_feeding_scrap` | Starts a new heat and starts scrap charging (manual scrap addition). |
| 15 | `sponge_feeder_onfurnace` | Ends scrap charging and starts sponge charging in the **same heat**. |
| 4 | `sponge_feeder_engaged` | Ends scrap charging and starts sponge charging in the **same heat**. |
| 6 | `sponge_feeder_initialheat` | Sponge feeder initial heat; transitions scrap to sponge. |
| 19 | `sponge_feeder_stand` | Sponge feeder stand in place; transitions scrap to sponge. |
| 27 | `magnet_moving_with_sponge` | Sponge feeding via magnet; transitions scrap to sponge. |
| 2 | `magnet_feeding_sponge` | Sponge feeding via magnet/bucket; transitions scrap to sponge. |
| 35 | `furnace_bottom_visiblepartially` | Inspection tilt / partial deslagging tilt during sponge charging; increments `tilt_count` when furnace returns to normal within 45s minimum gap. |
| 17 | `furnace_position_tilting` | Furnace movement away from normal position during sponge charging. |
| 18 | `furnace_bottom_visiblecompletely` | While sponge charging is active, opens 5-minute tapping candidate window. |
| 34 | `furnace_position_up` | Furnace tilted upward for pour; starts movement cycle. |
| 25 | `laddle_moving_withmeltiron` | Molten steel filled ladle; confirms tapping candidate immediately. |
| 36 | `laddle_placed_fortapping` | Ladle placed for tapping; confirms tapping candidate immediately. |
| 1 | `furnace_position_normal` | While tapping is active, ends tapping and completes the heat; also ends furnace movement cycle. |
| 5 | `furnace_operation_activefire` | Heartbeat used by the active-fire watchdog; resets timeout clock. |
| 41 / 42 | `human_feeding_silicomanganese` / `human_carring_silicomanganese` | Logged as alloy addition events (`silicomanganese_addition`) in `HeatEvents`. |
| 29 | `magnet_moving_slag` | Logged as slag removal events (`slag_removal`) in `HeatEvents`. |
| 28 | `furnace_operation_undermaintenance` | Flagged as maintenance in status and stream management. |
| 0 | `coil_position_placednearfurnace` | Direct maintenance detection class. |
| 7 | `furnace_operation_preheating` | Direct maintenance detection class. |
| 39 | `furma_position_placednearfurnace` | Direct maintenance detection class. |
| 31 | `furnace_operation_furmareplaced` | Direct maintenance detection class. |

### Startup mid-process recovery

If the application is first launched/restarted while the furnace is already in
sponge or tapping, it may recover that **one currently-running heat** from the
first class 15 / 4 / 6 / 19 / 27 / 2 / 17 / 18 / 34 / 25 / 36 seen. This recovery permission is consumed as soon as
that heat is created. After the heat finishes, lingering sponge/tilting/bottom-
visible detections cannot create another heat. The next heat must wait for a
new scrap detection (class 24, 21, or 37). This prevents phantom heats from
lingering equipment.

### Ending a heat

Once tapping has started, class 1 (`furnace_position_normal`) completes the
heat. If the next scrap detection (class 24, 21, or 37) arrives while tapping is still active,
the current heat is completed at that moment and the next heat begins immediately.
Additionally, if normal position is missed on camera, a 15-minute safety timeout
cap (`TAPPING_MAX_MINUTES`) automatically finalizes tapping and completes the heat.

### Active-fire watchdog (class 5, `furnace_operation_activefire`)

If a heat is in progress (any stage) and class 5 hasn't been confirmed
for configured timeout (`ACTIVEFIRE_TIMEOUT_MINUTES`) &mdash; counting from
the heat's own start if it's never been seen at all &mdash; the app
assumes the camera lost the fire (or the furnace isn't actually running)
and deletes that heat entirely: the SQL `Heats`/`HeatEvents` rows, the
per-heat CSV file, and its row in `heats_index.csv`. The app then drops
back to `IDLE`, ready for the next heat.

**Note:** this currently watches the whole heat, including scrap
charging. If scrap charging (or some other stage) can legitimately run
with no class-1 sighting on camera, every heat
will get wiped during that stage &mdash; tell me and this can be scoped to
start only once sponge charging begins instead.

### Assumptions I made where the spec was open-ended

- **Tilt/tap watching only starts during sponge charging**, and the
  130-minute fast track is likewise only evaluated while the state is
  `SPONGE_CHARGING`. If your furnace can also swing through these classes
  meaningfully during scrap charging, tell me and I'll widen this.
- **"2:13" is read as 2 minutes 13 seconds** (mm:ss), matching how the
  rest of the app displays durations. If you meant 2.13 minutes (2 min 8
  sec) instead, it's a one-line change to `TAPPING_BACKDATE` in
  `state_machine.py`.
- **The 15-minute tapping timer is checked continuously**, not just when a
  new class is detected: the camera worker checks it on every frame, and
  the status API checks it on every request, so a heat closes out on time
  even during a stretch with no relevant detections at all.
- If `magnet_feeding_scrap` arrives *after* the 15-minute mark has already
  passed, the timer is treated as having closed the previous heat first
  (capped at exactly +15 min), and the magnet detection starts a fresh
  heat from its own (later) timestamp rather than backdating anything.
- **Debouncing:** a class has to be seen on `DEBOUNCE_FRAMES` (default 5)
  consecutive *processed* frames before it's treated as "confirmed" and
  handed to the logic above. This is there to stop a single flickery frame
  from starting/ending a stage. Tune it in `config.py` if it feels too
  slow or too twitchy for your camera/model. The tilt cooldown is a
  second, independent layer on top of this, aimed specifically at rapid
  tilt/normal cycling rather than single-frame flicker.

If any of these read differently from what you had in mind, the state
machine is isolated in `state_machine.py` and covered by
`tests/test_state_machine.py`, so it's easy to adjust and re-verify.

## 4. CSV output

Each heat gets its own file: `data/heats/Heat_003_20260811_143000.csv`

```
event,start_time,end_time,duration_minutes,tilt_number
scrap_charging,2026-08-11T14:30:00,2026-08-11T14:52:00,22.0,
tilt,2026-08-11T15:10:00,2026-08-11T15:11:30,1.5,1
tilt_ignored_cooldown,2026-08-11T15:12:10,2026-08-11T15:12:40,0.5,
sponge_charging,2026-08-11T14:52:00,2026-08-11T16:10:00,78.0,
tapping,2026-08-11T16:10:00,2026-08-11T16:25:00,15.0,
```

`tilt_ignored_cooldown` rows are cycles that resolved within 2 minutes of
the previous *counted* tilt, so they show up in the CSV for audit purposes
but don't have a `tilt_number` and aren't reflected in `tilt_count`.

`data/heats_index.csv` keeps one summary row per heat (id, start, end,
total duration, tilt count, status, and the CSV filename) &mdash; that's what
powers the history table on the dashboard, and each row has a CSV download
link.

## 5. Performance tuning (`config.py`)

- `PROCESS_EVERY_N_FRAMES` &mdash; run YOLO on every Nth frame instead of
  every frame. The video stream itself always stays smooth; only how often
  new boxes/logic get computed changes. Raise this on CPU-only machines.
- `CONFIDENCE_THRESHOLD` &mdash; minimum confidence to consider a detection
  at all.
- `DEBOUNCE_FRAMES` &mdash; see above.
- `JPEG_QUALITY` &mdash; quality of the frames streamed to the browser.
- `MAX_STREAM_FPS` (top of `camera_worker.py`) &mdash; caps how fast
  `/video_feed` sends frames to the browser, independent of source fps.

## 6. Known limitations

- If the app is restarted mid-heat, that heat's CSV/index row stays as
  `IN_PROGRESS` and a fresh heat only starts on the next class-3 detection
  &mdash; in-flight stage tracking is not persisted across restarts, only the
  heat numbering is (it reads the last used heat number back from
  `heats_index.csv` on startup).
- This has been built and unit-tested against the state-machine logic
  itself (`tests/test_state_machine.py`) and smoke-tested end-to-end with
  the RTSP/YOLO pieces mocked out, since I don't have your actual camera
  stream or `best.pt` weights to test against here. Point it at your real
  stream and watch the "Last event" line on the dashboard while a heat runs
  to confirm the transitions line up with what you see on camera, and tune
  `CONFIDENCE_THRESHOLD` / `DEBOUNCE_FRAMES` from there.
---

## SQL Server recording

Every row the app writes to a heat CSV is also pushed to SQL Server. The
CSVs are unchanged and still written first, so the database is an addition,
never a dependency - pull the network cable and the app carries on.

### Setup

1. `pip install pyodbc` and install the *ODBC Driver 17 (or 18) for SQL
   Server* on the machine running the app.
2. Set the connection details as environment variables (preferred) or edit
   the `SQL Server` block in `config.py`:

   ```bash
   export SQL_SERVER="13.200.63.36,1433"
   export SQL_DATABASE="Furnace"
   export SQL_UID="furnace_app"
   export SQL_PASSWORD="..."
   ```
3. `python3 check_sql.py` to confirm the login works.
4. `python3 app.py`. Tables, indexes and views are created on first
   connect, and today's heats are synchronized in the background.

Set `SQL_ENABLED=0` to turn the whole SQL path off and run CSV-only.

### Date Filtering (Today and Now Only)

By default, historical CSV records from prior days are **excluded** from database synchronization:
* `SYNC_TODAY_ONLY=1` (default in [config.py](file:///c:/Users/shubh/PythonProjects/MSP_Digital/Updated_version/config.py)): Background backfill and default migrations only share data from today and live ongoing heats ("now").
* `SYNC_SINCE_DATE="YYYY-MM-DD"`: Optional override to sync heats on or after a specific date.

### Threads

| Thread | Owns | Notes |
| --- | --- | --- |
| `CameraWorker` | RTSP + YOLO + state machine | Never touches the database |
| `SqlRecorder` | The one SQL connection | Fed by a queue; writes live events in real-time, retries, spools |
| `SqlBackfill` | Background CSV sync on startup | Only syncs today's heats by default; skips older history |

The state machine calls `record_*()`, which puts a small dict on a queue
and returns - measured at well under a millisecond per heat event, with the
database deliberately unplugged. Nothing on the camera or request path ever
waits on SQL Server.

### When the database is down

Rows go to `data/sql_spool.jsonl` instead, and are replayed **in order**
the moment the connection comes back; the file is deleted once drained.
Event keys are derived from `(heat_id, row number, event name)`, and every
insert is guarded by `WHERE NOT EXISTS`, so replaying the spool - or
re-running the CSV backfill - can't create duplicates.

Check on it any time:

```bash
curl http://localhost:8000/api/sql/status
```

```jsonc
{
  "recorder": {"connected": true, "pending": 0, "spooled": 0,
               "rows_written": 42, "rows_replayed": 3, "last_error": null},
  "backfill": {"state": "done", "heats_written": 2, "events_written": 14}
}
```

### Querying it

`dbo.vw_HeatAnalysis` is one row per heat with the three stage durations
pivoted out, tilt counts, unaccounted minutes and within-expected-range
flags. `dbo.vw_HeatDaily` rolls that up per day. Point Power BI or SSMS at
either one; see `sql/analysis_queries.sql` for worked examples.

To run CSV sync manually:

```bash
python3 migrate_csv_to_sql.py --dry-run        # preview today's heats to sync
python3 migrate_csv_to_sql.py                  # sync today's heats only (default)
python3 migrate_csv_to_sql.py --all            # sync all historical CSVs
python3 migrate_csv_to_sql.py --since 2026-09-20  # sync heats on/after date
```

