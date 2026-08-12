# Furnace process monitor

Turns `best.pt` detections into scrap charging / sponge charging / tapping
timing, plus a per-heat tilt count, and shows it on a live dashboard next to
the annotated RTSP feed.

## Files

| File | What it does |
|---|---|
| `process_tracker.py` | The state machine. No video/network code - pure logic, easy to unit test. |
| `inference_loop.py` | Reference glue between `best.pt` and `ProcessTracker`. Uses plain OpenCV `VideoCapture` so it runs standalone - **swap the frame source for your existing GStreamer pipeline**, everything else stays. |
| `server.py` | FastAPI backend: MJPEG video feed, `/api/status`, `/api/history`, `/ws/status`. |
| `static/index.html` | The dashboard (video + live process rail + heat log). Single file, no build step. |

## Run it

```bash
pip install -r requirements.txt
# edit RTSP_URL in server.py, and MODEL_PATH if best.pt isn't alongside it
uvicorn server:app --host 0.0.0.0 --port 8000
```

Then open `http://<host>:8000/`.

## How the 5 classes turn into 3 processes + a tilt count

You only care about 5 of the 27 classes:

```
magnet_feeding_scrap     -> Scrap charging   (stage 1)
spange_feeder_engaged    -> Sponge charging  (stage 2)
furnace_position_up      -> Tapping          (stage 3)
furnace_position_tilding -> Tilting          (a count, 1-5x per heat)
furnace_position_normal  -> Idle / baseline
```

**The size-30 rolling window, exactly as you described it:** every frame,
the top relevant detection is appended to a `deque(maxlen=30)`. Once the
window is full, whichever label has the most entries ("max") becomes the
smoothed current state. Because it's a majority vote over a rolling list
rather than a single frame, one noisy/missed detection can't flip the
displayed process - and when a tilt ends, the vote flips back to sponge
charging on its own as fresh sponge-charging frames refill the window. No
extra "resume" logic needed.

**One deliberate deviation from your spec, worth flagging explicitly:**
`furnace_position_up` (tapping) is **not** in that size-30 window - only
`magnet_feeding_scrap`, `spange_feeder_engaged`, `furnace_position_normal`
and `furnace_position_tilding` are, which is 4 classes, matching what you
described. Reasoning: tapping is the event that ends the heat, and it needs
to be caught fast and cleanly. If it sat in the same 30-frame vote as several
minutes of sponge-charging history, the vote would still be dominated by
"sponge_charging" for a long stretch after the furnace actually tips up,
delaying the transition right when you most want it to be crisp. So tapping
gets its own short consecutive-frame confirmation instead (see below). If
you actually want it folded into the same 30-frame window as the other four,
that's a small change - say so and I'll adjust `process_tracker.py`.

**Tapping as a real, timed stage:** a pour takes a few minutes, so tapping
isn't treated as an instant marker. `furnace_position_up` has to be detected
for `TAPPING_CONFIRM_FRAMES` (default 5) consecutive updates to *start* the
tapping stage, and has to *stop* being detected for the same number of
consecutive updates to close it out and end the heat. That gives tapping a
live duration on the dashboard, same as the other two stages.

**Tilt count:** each time the smoothed state transitions *into* "tilting"
(not each frame it's detected), `tilt_count` for the current heat goes up by
one. It resets to 0 when a new heat starts.

**Heat lifecycle:** a heat is created the first time the window settles on
`scrap_charging`. It stays open through sponge charging and any tilts, and
closes the moment tapping is confirmed to have ended. The next heat starts
fresh (tilt count back to 0, window cleared) the next time scrap charging
wins the vote.

## Tuning

The constants at the top of `process_tracker.py`:

```python
WINDOW_SIZE = 30            # as specified
MIN_VOTES_TO_SWITCH = 16    # simple majority of 30
TAPPING_CONFIRM_FRAMES = 5  # consecutive frames to start/end tapping
```

These are defined in **updates**, not seconds - `inference_loop.run()` takes
a `max_calls_per_second` argument (default 5) that throttles how often
`tracker.update()` is actually called, independent of your camera's real
frame rate or GPU inference speed. That keeps "30 frames" meaning a
predictable ~6 seconds of history regardless of RTSP jitter. Two things to
tune once you're watching it against real footage:

- If the current process badge is slow to change or gets "stuck" behind a
  brief interruption, lower `MIN_VOTES_TO_SWITCH` or `WINDOW_SIZE`.
- If it flickers between states, raise them, or lower `max_calls_per_second`
  so each of the 30 slots covers more real time.
- If tapping is confirmed too eagerly/late, adjust `TAPPING_CONFIRM_FRAMES`
  (this affects both the start and end confirmation).

## Wiring in your existing GStreamer pipeline

`ProcessTracker` doesn't know anything about video - it only needs:

```python
tracker.update(detected_class_name_or_None, confidence)
```

So from wherever your current `_annotate()` function receives YOLO results
per frame in your GStreamer appsink callback, add the same two lines
`inference_loop.py` uses:

```python
cls_name, conf = top_relevant_detection(result, model.names)
tracker.update(cls_name, conf)
```

`top_relevant_detection()` in `inference_loop.py` also resolves the case
where more than one of the 5 classes fires on the same frame (e.g. the
furnace sits in a normal position while the magnet is simultaneously
feeding scrap) using a priority order - tapping always wins outright, then
an actual activity class beats the "normal" baseline.

## Dashboard notes

- `/video_feed` is MJPEG - simplest thing that works given you're already
  producing annotated frames in Python, and it doesn't need a transcoding
  step. If you eventually need lower bandwidth for many simultaneous
  viewers, swap it for an HLS (ffmpeg) or WebRTC output later; the rest of
  the dashboard (status rail, heat log) doesn't change either way.
- The status rail talks to `/ws/status` (pushed every second) and falls back
  to polling `/api/status` if the socket drops, so it keeps working on a
  flaky local network.
- `/api/history` only holds heats completed since the server last started -
  it's in-memory. If you want heats to survive a restart, the natural next
  step is writing each closed `Heat.to_dict()` to InfluxDB or a small SQLite
  file in `ProcessTracker._end_heat()`.
