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

## 3. What triggers what

The logic matches on class **names**, never numeric class IDs - a class's
number can change every time the model is retrained (and it has: the class
map below no longer matches the numbers from earlier versions of this app)
without touching a single line of code, as long as the name stays the same.

Every class that drives a transition is wired up in **one place**:
`config.py`'s `PROCESS_CLASSES` dict. `state_machine.py` reads its `CLS_*`
names from there and never hardcodes a class string itself, so if a
retrain renames a class - or you simply want a different signal to drive a
step - it's a one-line edit in `config.py`, nothing else needs to change:

```python
# config.py
PROCESS_CLASSES = {
    "scrap_charging_start":       "magnet_feeding_scrap",
    "scrap_charging_magnet_alt":  "magnet_moving_withscrap_above_furnace_normal",
    "sponge_charging_start":      "spange_feeder_onfurnace",
    "sponge_charging_start_alt":  "spange_feeder_engaged",
    "furnace_normal":             "furnace_position_normal",
    "furnace_tilting":            "furnace_position_tilding",
    "furnace_activefire":         "furnace_operation_activefire",
}
```

All the timing thresholds below are likewise plain numbers at the bottom
of `config.py` (`SCRAP_CHARGING_MAX_MINUTES`, `SPONGE_CHARGING_MAX_MINUTES`,
`SPONGE_FAST_TRACK_MINUTES`, `TILT_TO_TAPPING_MINUTES`,
`TAPPING_MAX_MINUTES`, `ACTIVEFIRE_TIMEOUT_MINUTES`) - change the value
there, restart the app, done.

| Role (`PROCESS_CLASSES` key) | Default class name | Effect |
|---|---|---|
| `scrap_charging_start` | `magnet_feeding_scrap` | Starts a **new heat**; starts the *scrap charging* clock. If this fires while the *previous* heat is still in `TAPPING`, that heat is closed out immediately and the new heat starts from this same moment. |
| `scrap_charging_magnet_alt` | *(derived, not a model class)* | Same effect as `scrap_charging_start` above. Computed every frame from the raw `magnet_moving_withscrap` and `furnace_position_normal` boxes - true when the magnet box sits just above the furnace box (see "Scrap charging: the magnet-above-furnace signal" below). |
| `sponge_charging_start` | `spange_feeder_onfurnace` | Ends *scrap charging*, starts the *sponge charging* clock. |
| `sponge_charging_start_alt` | `spange_feeder_engaged` | Same effect as `sponge_charging_start` above - either class ends scrap charging. |
| `furnace_tilting` | `furnace_position_tilding` | The **only** signal tapping reacts to - see "Tapping: the only two ways in" below. |
| `furnace_normal` | `furnace_position_normal` | While `TAPPING`: ends tapping, heat complete. While *watching* a tilt: resolves it back to "just a tilt" (see below). |
| `furnace_activefire` | `furnace_operation_activefire` | Heartbeat only. Resets the active-fire watchdog clock; doesn't drive a transition on its own. |

All other classes (ladle, negative samples, etc.) are detected and drawn
on the video for your own reference, but don't drive any of the timers.

### Scrap charging: the magnet-above-furnace signal

`magnet_moving_withscrap` on its own doesn't mean anything to the state
machine. Every processed frame, `camera_worker.py` checks whether a
`magnet_moving_withscrap` box sits **just above** a `furnace_position_normal`
box in that same frame (vertical gap under `MAGNET_ABOVE_FURNACE_MAX_GAP_PX`,
80px by default, and horizontal overlap over `MAGNET_ABOVE_FURNACE_MIN_OVERLAP_RATIO`,
15% by default) and, if so, feeds the derived class name into the state
machine exactly like any other confirmed detection. Tune those two
`config.py` values once you can see it against real footage.

### Tapping: the only two ways in

Tapping starts **only** when one of these two things happens - nothing
else starts it:

1. `furnace_tilting` is held open for more than `TILT_TO_TAPPING_MINUTES`
   (**3.5 min** by default) without `furnace_normal` ever arriving to
   resolve it &mdash; promoted straight into tapping, backdated to when
   the tilt itself began. A pure time check, so it fires from
   `check_timeouts()` even with no further detection at all.
2. `furnace_tilting` is detected once sponge charging has already run for
   `SPONGE_FAST_TRACK_MINUTES` (**130 min** by default) or more &mdash;
   taken as tapping starting immediately, no need to also wait out
   `TILT_TO_TAPPING_MINUTES` first (the "fast track").

Anywhere under the 130-minute mark, a `furnace_tilting` sighting followed
by `furnace_normal` inside `TILT_TO_TAPPING_MINUTES` is just a tilt:
`tilt_count += 1`, and the app keeps waiting inside sponge charging.

### Ending a heat

Tapping (and the heat) ends whichever of these happens first:

- `furnace_normal` is detected while `TAPPING`, or
- `TAPPING_MAX_MINUTES` (**7 min** by default) have passed since tapping
  started with no `furnace_normal` detected &mdash; a hard cap, logged
  with a note, purely time-based so it fires even with no detections at
  all during that stretch, or
- `scrap_charging_start` (`magnet_feeding_scrap`, or the magnet-above-
  furnace signal) is detected &mdash; which also means the *next* heat's
  scrap charging has begun, so a new heat starts immediately from that
  same moment.

### Scrap/sponge charging hard caps

Independent of everything above:

- Scrap charging cannot run longer than `SCRAP_CHARGING_MAX_MINUTES`
  (**40 min** by default). If no `sponge_charging_start` signal has
  arrived by then, the state machine forces the transition into sponge
  charging anyway.
- Sponge charging cannot run longer than `SPONGE_CHARGING_MAX_MINUTES`
  (**140 min** by default). If tapping hasn't started by then, the state
  machine forces the transition into tapping anyway. In practice this is
  just a backstop behind the 130-minute fast track above - it only fires
  if no `furnace_tilting` sighting ever arrived to trigger the fast track
  itself.

Both are pure time-based checks (`check_timeouts()`), so they fire even
with no detections at all during that stretch.

### Active-fire watchdog (`furnace_activefire`)

If a heat is in progress (any stage) and `furnace_activefire` hasn't been
confirmed for `ACTIVEFIRE_TIMEOUT_MINUTES` (50 min by default) &mdash;
counting from the heat's own start if it's never been seen at all &mdash;
the app assumes the camera lost the fire (or the furnace isn't actually
running) and deletes that heat entirely: the SQL `Heats`/`HeatEvents`
rows, the per-heat CSV file, and its row in `heats_index.csv`. The app
then drops back to `IDLE`, ready for the next heat, and the next detection
starts a brand new one (heat numbers aren't reused, so the deleted heat
leaves a gap rather than a duplicate).

**Note:** this currently watches the whole heat, including scrap
charging. If scrap charging (or some other stage) can legitimately run
longer than `ACTIVEFIRE_TIMEOUT_MINUTES` with no active-fire sighting on
camera, that stage's heats will get wiped &mdash; tell me and this can be
scoped to only start once sponge charging begins.

### Assumptions I made where the spec was open-ended

- **Tilt/tapping watching only starts during sponge charging**, and the
  130-minute fast track is likewise only evaluated while the state is
  `SPONGE_CHARGING`. If your furnace can also swing through
  `furnace_tilting` meaningfully during scrap charging, tell me and I'll
  widen this.
- **All the time-based transitions are checked continuously**, not just
  when a new class is detected: the camera worker checks them on every
  frame, and the status API checks them on every request, so a tilt gets
  promoted to tapping (or a hard cap fires, or a heat gets deleted by the
  active-fire watchdog) on time even during a stretch with no relevant
  detections at all.
- **Debouncing:** a class has to be seen on `DEBOUNCE_FRAMES` (default 5)
  consecutive *processed* frames before it's treated as "confirmed" and
  handed to the logic above. This is there to stop a single flickery frame
  from starting/ending a stage. Tune it in `config.py` if it feels too
  slow or too twitchy for your camera/model.

If any of these read differently from what you had in mind, the state
machine is isolated in `state_machine.py` and covered by
`tests/test_state_machine.py`, so it's easy to adjust and re-verify.

## 4. CSV output

Each heat gets its own file: `data/heats/Heat_003_20260811_143000.csv`

```
event,start_time,end_time,duration_minutes,tilt_number,note
scrap_charging,2026-08-11T14:30:00,2026-08-11T14:52:00,22.0,,
tilt,2026-08-11T15:10:00,2026-08-11T15:11:30,1.5,1,
sponge_charging,2026-08-11T14:52:00,2026-08-11T16:10:00,78.0,,
tapping,2026-08-11T16:10:00,2026-08-11T16:15:30,5.5,,
```

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

## 5b. Process logic tuning (`config.py`) - new model / different classes

Everything the state machine reacts to is configurable, without touching
any code:

- **`CLASS_NAMES`** &mdash; the model's numeric-ID-to-name map. Only used as
  a fallback for a model that doesn't embed its own names (`detector.py`
  prefers the names baked into the `.pt` weights when present). Update this
  whenever you swap in a retrained model with a different class list/order.
- **`PROCESS_CLASSES`** &mdash; which class NAME drives each step (scrap
  charging start + its magnet-above-furnace alt, sponge charging start +
  its alt, furnace normal/tilting, active fire). This is the dict to edit
  if a retrain renames a class, or you simply want a different signal to
  drive a step - see section 3 above.
- **Timings** &mdash; `SCRAP_CHARGING_MAX_MINUTES`, `SPONGE_CHARGING_MAX_MINUTES`,
  `SPONGE_FAST_TRACK_MINUTES`, `TILT_TO_TAPPING_MINUTES`,
  `TAPPING_MAX_MINUTES`, `ACTIVEFIRE_TIMEOUT_MINUTES` &mdash; also plain
  numbers in `config.py`, right below `PROCESS_CLASSES`.
- **Magnet-above-furnace spatial thresholds** &mdash;
  `MAGNET_ABOVE_FURNACE_MAX_GAP_PX`, `MAGNET_ABOVE_FURNACE_MIN_OVERLAP_RATIO`.

Restart the app after editing any of these for the change to take effect.

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
   connect, and the CSVs already on disk are loaded in the background.

Set `SQL_ENABLED=0` to turn the whole SQL path off and run CSV-only.

### Threads

| Thread | Owns | Notes |
| --- | --- | --- |
| `CameraWorker` | RTSP + YOLO + state machine | Never touches the database |
| `SqlRecorder` | The one SQL connection | Fed by a queue; writes, retries, spools |
| `SqlBackfill` | One-shot CSV import at startup | Retries with backoff, then gives up quietly |

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
  "backfill": {"state": "done", "heats_written": 8, "events_written": 123}
}
```

### Querying it

`dbo.vw_HeatAnalysis` is one row per heat with the three stage durations
pivoted out, tilt counts, unaccounted minutes and within-expected-range
flags. `dbo.vw_HeatDaily` rolls that up per day. Point Power BI or SSMS at
either one; see `sql/analysis_queries.sql` for worked examples.

To load history manually (safe to repeat):

```bash
python3 migrate_csv_to_sql.py --dry-run   # show what would load
python3 migrate_csv_to_sql.py             # load it
```
