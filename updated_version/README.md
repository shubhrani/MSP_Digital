# Furnace Process Monitor

A Flask dashboard that watches an RTSP camera feed with a YOLOv11
*segmentation* model (`best.pt`), figures out which stage of the
steelmaking heat cycle the furnace is in (scrap charging &rarr; sponge
charging &rarr; tapping), counts furnace tilts that are *not* tapping, and
logs the start/end time of every stage to a CSV file per heat.

```
furnace_monitor/
├── app.py              Flask app: routes, video stream, JSON APIs
├── camera_worker.py    Background thread: RTSP read + YOLO + drawing
├── detector.py         Thin YOLO wrapper + segmentation-mask drawing helpers
├── state_machine.py    The actual process logic (pure Python, unit tested)
├── debounce.py         Filters single-frame detection flicker
├── config.py           All the settings you're likely to want to change
├── templates/dashboard.html
├── static/css/style.css
├── static/js/dashboard.js
├── models/              <- put best.pt here
├── data/heats/           one CSV per heat is written here
├── data/heats_index.csv  one summary row per heat (created automatically)
└── tests/test_state_machine.py   run with `python3 tests/test_state_machine.py`
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

The logic matches on the **numeric class IDs**, not the label text, so it
keeps working even if your model's names differ slightly from the list you
gave me. All the timing numbers below (2 min cooldown, 130 min, 2:13
backdate, 15 min) are module-level constants at the top of
`state_machine.py` (`TILT_COOLDOWN_MINUTES`, `SPONGE_FAST_TRACK_MINUTES`,
`TAPPING_BACKDATE`, `TAPPING_MAX_MINUTES`) if you ever need to tune them.

| ID | Class | Effect |
|----|-------|--------|
| 3  | `magnet_feeding_scrap` | Starts a **new heat**; starts the *scrap charging* clock. If this fires while the *previous* heat is still in `TAPPING`, that heat is closed out immediately (see "Ending a heat" below) and the new heat starts from this same moment. |
| 8  | `spange_feeder_onfurnace` | Ends *scrap charging*, starts the *sponge charging* clock. |
| 12 / 18 | `furnace_position_tilding` / `furnace_position_up` | **Under 130 minutes of sponge charging:** furnace leaves the "normal" position &mdash; the app starts watching to see whether this turns into tapping or is just a tilt. **At 130+ minutes:** immediately confirms tapping (fast track, see below). |
| 16 | `furnace_bottom_visiblecompletely` | **Under 130 minutes:** if seen while the furnace is being watched, confirms tapping &mdash; *sponge charging* is closed out at the moment the furnace first moved, and the *tapping* clock starts from that same moment. **At 130+ minutes:** also triggers the fast track on its own, even without an open watch. |
| 13 | `furnace_bottom_visiblepartially` | **Under 130 minutes:** tracked for information only while watching a tilt, doesn't gate a transition. **At 130+ minutes:** also triggers the fast track on its own. |
| 2  | `furnace_position_normal` | Furnace is back to normal while being watched. Tapping was never confirmed for this watch &rarr; it was just a tilt: tilt count **+1**, unless a tilt was already counted within the last 2 minutes (see "Tilt cooldown"). The app keeps waiting inside sponge charging for the real tapping sequence. |

All other classes (magnet movement, ladle, negative samples, etc.) are
detected and drawn on the video for your own reference, but don't drive
any of the timers.

### The 130-minute fast track into tapping

Once sponge charging has been running for **130 minutes or more**, the
first of 12 / 13 / 16 / 18 to show up is taken as tapping having started
&mdash; there's no need to also wait for the furnace to come back to
"normal" and confirm via class 16 the way the under-130-minute path does.

Because the visual transition takes a little while to fully register, the
sponge-charging/tapping boundary is **backdated by 2 minutes 13 seconds**
from the moment of detection: that time is *added* to tapping and
*deducted* from sponge charging, and both stage's start/end times are
adjusted accordingly.

### Tilt cooldown

Once a tilt has been **counted**, any further "furnace leaves normal,
comes back without tapping" cycle that resolves within **2 minutes** of
that counted tilt is treated as a continuation of the same movement
(camera/model flicker) rather than a brand-new one, so `tilt_count`
doesn't climb rapidly. These suppressed cycles are still written to the
heat's CSV as `tilt_ignored_cooldown` rows for audit purposes &mdash; they
just don't increment the count or the `tilt` event rows.

### Ending a heat

Tapping (and the heat) ends on **whichever of these happens first**:

- **15 minutes** have passed since tapping started, or
- class 3 (`magnet_feeding_scrap`) is detected &mdash; which also means the
  *next* heat's scrap charging has begun, so a new heat starts immediately
  from that same moment.

This replaces the older "furnace back to normal after class 16" ending:
once tapping is confirmed (either the under-130-min or the fast-track
way), class 2/12/13/16/18 no longer drive any transition &mdash; only the
timer and class 3 close out the heat.

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