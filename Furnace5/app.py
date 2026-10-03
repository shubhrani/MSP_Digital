"""
Furnace Monitoring Dashboard - Flask app.

Run with:
    python3 app.py

Then open http://<this-machine-ip>:8000 in a browser. "/" redirects to the
default furnace; both furnaces have their own page:

    http://<this-machine-ip>:8000/furnace/4-1
    http://<this-machine-ip>:8000/furnace/4-2

Physical cameras and PYRO sources are dynamically managed by FurnaceManager
(furnace_manager.py), which decouples the physical streams from logical
furnace slots (4/1 and 4/2).
"""

import atexit
import json
import logging
import os
import threading
import time

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for, abort
import cv2
import numpy as np

import config
import db
import db_sync
from furnace_manager import FurnaceManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("furnace_app")

app = Flask(__name__)

# One connection/queue shared by every furnace - each row it writes carries
# its own FurnaceId, so one recorder is all that's needed no matter how many
# furnaces this process is watching.
sql_recorder = db.SqlRecorder()
manager = FurnaceManager(recorder=sql_recorder)


def _make_placeholder_frame(message="Connecting camera feed..."):
    frame = np.zeros((480, 854, 3), dtype="uint8")
    frame[:] = (17, 19, 23)
    cv2.putText(
        frame, message, (40, 240), cv2.FONT_HERSHEY_SIMPLEX,
        0.9, (138, 147, 160), 2, cv2.LINE_AA,
    )
    ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY])
    return jpeg.tobytes() if ok else b""


def _get_slot_or_404(slug):
    slot = manager.get_slot(slug)
    if slot is None:
        abort(404)
    return slot


@app.route("/")
def home():
    if config.DEFAULT_FURNACE_SLUG in manager.slots:
        return redirect(url_for("dashboard", slug=config.DEFAULT_FURNACE_SLUG))
    return render_template("home.html", furnaces=manager.nav_slots())


@app.route("/furnace/<slug>")
def dashboard(slug):
    slot = _get_slot_or_404(slug)
    return render_template(
        "dashboard.html",
        furnace_slug=slot.slug,
        furnace_name=slot.name,
        furnace_id=slot.furnace_id,
        nav_furnaces=manager.nav_slots(),
    )


def _mjpeg_generator(slot):
    """Streams frames from whichever CameraWorker is *currently* attached
    to the slot. If the slot is mid-swap (maintenance -> spare), we yield
    a placeholder frame so the browser's <img> never goes blank, and wait
    for the new worker to bind and stream."""
    boundary = b"--frame"
    while True:
        worker = slot.camera_worker
        if worker is None:
            # No worker attached yet (mid-swap or initializing).
            # Yield placeholder so browser doesn't show blank/broken feed.
            placeholder = _make_placeholder_frame(f"Connecting feed for {slot.name}...")
            if placeholder:
                yield (boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n"
                       + placeholder + b"\r\n")
            time.sleep(0.5)
            continue
        # Stream from this worker until it stops (i.e. the slot is swapped).
        for jpeg in worker.frames():
            yield (boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n"
                   + jpeg + b"\r\n")
        # The worker exited. The slot may already have a new one (swap
        # already happened) or not yet (still attaching); loop back.
        time.sleep(0.25)


@app.route("/furnace/<slug>/video_feed")
def video_feed(slug):
    slot = _get_slot_or_404(slug)
    return Response(_mjpeg_generator(slot),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/furnace/<slug>/api/status")
def api_status(slug):
    slot = _get_slot_or_404(slug)
    status = slot.state_machine.get_status() if slot.state_machine else {}
    status["camera_connected"] = slot.camera_worker.is_connected() if slot.camera_worker else False
    status["model_error"] = slot.camera_worker.detector_error if slot.camera_worker else None
    status["furnace_id"] = slot.furnace_id
    status["furnace_name"] = slot.name

    status["temperature"] = slot.temperature_monitor.get_latest()
    status["stream"] = {
        "stream_id": slot.current_stream.stream_id if slot.current_stream else None,
        "name":      slot.current_stream.name      if slot.current_stream else None,
        "pyro_field": slot.pyro_field,
    } if slot.current_stream else None

    status["all_streams"] = manager.streams_snapshot()
    status["all_temperatures"] = {
        slug_key: {
            "furnace_name": s.name,
            "pyro_field": s.pyro_field,
            "assigned_stream": s.current_stream.stream_id if s.current_stream else None,
            **s.temperature_monitor.get_latest(),
        }
        for slug_key, s in manager.slots.items()
    }
    return jsonify(status)


@app.route("/furnace/<slug>/api/heats")
def api_heats(slug):
    slot = _get_slot_or_404(slug)
    return jsonify(slot.state_machine.get_heats_history() if slot.state_machine else [])


@app.route("/furnace/<slug>/api/heats/<heat_id>/csv")
def api_heat_csv(slug, heat_id):
    slot = _get_slot_or_404(slug)
    path = slot.state_machine.get_heat_csv_path(heat_id) if slot.state_machine else None
    if not path:
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{heat_id}.csv")


@app.route("/furnace/<slug>/api/frames")
def api_frames(slug):
    """List debug frames captured for a specific heat, all heats, or the active heat.
    Query parameters:
      - heat_id: specific heat id, 'all' for all heats, or omit for active/latest
      - limit: maximum number of frames to return (default 100)
    """
    slot = _get_slot_or_404(slug)
    root = config.FRAMES_DIR
    if not os.path.isdir(root):
        return jsonify([])

    requested_heat = (request.args.get("heat_id") or "").strip()
    limit = int(request.args.get("limit", 100))

    if requested_heat and requested_heat.lower() == "all":
        heat_dirs = sorted(
            (d for d in os.listdir(root)
             if os.path.isdir(os.path.join(root, d))),
            reverse=True,
        )
    elif requested_heat:
        safe_hid = os.path.basename(requested_heat)
        heat_dirs = [safe_hid] if os.path.isdir(os.path.join(root, safe_hid)) else []
    else:
        active_heat_id = slot.state_machine.heat_id if slot.state_machine else None
        if active_heat_id and os.path.isdir(os.path.join(root, active_heat_id)):
            heat_dirs = [active_heat_id]
        else:
            prefix = slot.heat_id_prefix
            candidate_dirs = sorted(
                (d for d in os.listdir(root)
                 if os.path.isdir(os.path.join(root, d))),
                reverse=True,
            )
            matching = [d for d in candidate_dirs if d.startswith(prefix)]
            heat_dirs = matching[:1] if matching else candidate_dirs[:1]

    frames = []
    for hid in heat_dirs:
        dir_path = os.path.join(root, hid)
        if not os.path.isdir(dir_path):
            continue
        for name in sorted(os.listdir(dir_path), reverse=True):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(dir_path, name), encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            meta["heat_id"] = hid

            # Provide both annotated and raw frame URLs
            filename_ann = meta.get("filename_annotated") or meta.get("filename", "")
            filename_raw = meta.get("filename_raw") or meta.get("filename", "")

            meta["url_annotated"] = url_for("api_frame_file", slug=slug,
                                            heat_id=hid, filename=filename_ann) if filename_ann else None
            meta["url_raw"] = url_for("api_frame_file", slug=slug,
                                      heat_id=hid, filename=filename_raw) if filename_raw else None
            meta["url"] = meta["url_annotated"] or meta["url_raw"]
            meta["url_json"] = url_for("api_frame_file", slug=slug,
                                       heat_id=hid, filename=name)
            meta["json_filename"] = name

            frames.append(meta)
            if len(frames) >= limit:
                break
        if len(frames) >= limit:
            break

    frames.sort(key=lambda m: m.get("timestamp") or "", reverse=True)
    return jsonify(frames)


@app.route("/furnace/<slug>/api/frames/heats")
def api_frames_heats(slug):
    """List all heat directories that contain captured debug frames."""
    slot = _get_slot_or_404(slug)
    root = config.FRAMES_DIR
    if not os.path.isdir(root):
        return jsonify([])

    current_heat_id = slot.state_machine.heat_id if slot.state_machine else None
    prefix = slot.heat_id_prefix

    heats_info = []
    for hid in sorted(os.listdir(root), reverse=True):
        dir_path = os.path.join(root, hid)
        if not os.path.isdir(dir_path):
            continue
        json_files = [f for f in os.listdir(dir_path) if f.endswith(".json")]
        if not json_files:
            continue

        latest_ts = ""
        for jf in sorted(json_files, reverse=True)[:3]:
            try:
                with open(os.path.join(dir_path, jf), encoding="utf-8") as f:
                    data = json.load(f)
                    ts = data.get("timestamp") or data.get("saved_at") or ""
                    if ts > latest_ts:
                        latest_ts = ts
            except Exception:
                pass

        is_relevant = (
            hid.startswith(prefix)
            or f"_{slot.furnace_id.replace('/', '_')}" in hid
            or hid == current_heat_id
        )

        heats_info.append({
            "heat_id": hid,
            "frame_count": len(json_files),
            "latest_timestamp": latest_ts,
            "is_current": (hid == current_heat_id),
            "is_relevant_slot": is_relevant,
        })

    heats_info.sort(
        key=lambda h: (h["is_current"], h["is_relevant_slot"], h["latest_timestamp"] or h["heat_id"]),
        reverse=True,
    )
    return jsonify(heats_info)


@app.route("/furnace/<slug>/api/frames/<heat_id>/<filename>")
def api_frame_file(slug, heat_id, filename):
    _get_slot_or_404(slug)
    heat_id = os.path.basename(heat_id)     # never trust path segments
    filename = os.path.basename(filename)
    path = os.path.join(config.FRAMES_DIR, heat_id, filename)
    if not os.path.isfile(path):
        abort(404)
    if filename.endswith(".json"):
        mimetype = "application/json"
    elif filename.endswith(".png"):
        mimetype = "image/png"
    else:
        mimetype = "image/jpeg"
    return send_file(path, mimetype=mimetype)


@app.route("/furnace/<slug>/api/sql/status")
def api_sql_status(slug):
    """Is the database keeping up? Reads in-memory counters only - it does
    not touch SQL Server, so it stays fast even when the server is down."""
    slot = _get_slot_or_404(slug)
    return jsonify({"recorder": sql_recorder.status(), "backfill": db_sync.status(slot.slug)})


@app.route("/api/streams")
def api_streams():
    """Global view of all three physical streams and their status."""
    return jsonify(manager.streams_snapshot())


def _timeout_ticker(slot, stop_event):
    """Runs check_timeouts() every second, independent of the camera.
    Keeps the process clock ticking during a camera outage, so a heat
    isn't frozen in one state for the whole downtime and then jumps on
    reconnect."""
    while not stop_event.wait(1.0):
        try:
            if slot.state_machine is not None:
                slot.state_machine.check_timeouts()
        except Exception:
            logger.exception("Timeout tick failed for %s", slot.slug)


def _startup():
    """Start the background workers. Recorder starts, manager probes and
    binds slots, then background backfill handles each slot."""
    sql_recorder.start()
    atexit.register(sql_recorder.stop)

    manager.start()
    atexit.register(manager.stop)

    for slug, slot in manager.slots.items():
        db_sync.start_background_backfill(
            slug,
            data_dir=slot.data_dir,
            index_csv=slot.index_csv,
            furnace_id=slot.furnace_id,
            state_machine=slot.state_machine,
        )

        stop_evt = threading.Event()
        threading.Thread(
            target=_timeout_ticker,
            args=(slot, stop_evt),
            name=f"TimeoutTick-{slug}",
            daemon=True,
        ).start()


def _run_supervisor():
    """Supervises the furnace application process.
    If the worker process crashes (e.g. Segmentation fault / signal 11 / exit code 139 / unhandled error),
    this supervisor catches the crash, logs it, and immediately auto-restarts the application with python3 app.py.
    A clean exit (Ctrl+C, SIGTERM, or exit code 0) terminates normally without restarting.
    """
    import subprocess
    import sys
    import time

    env = os.environ.copy()
    env["FURNACE_SUPERVISOR_CHILD"] = "1"

    cmd = [sys.executable] + sys.argv
    logger.info("Furnace Auto-Restart Supervisor active. Monitoring process: %s", " ".join(cmd))

    consecutive_fast_crashes = 0
    while True:
        start_time = time.monotonic()
        try:
            p = subprocess.Popen(cmd, env=env)
            exit_code = p.wait()
        except KeyboardInterrupt:
            logger.info("Supervisor received KeyboardInterrupt (Ctrl+C). Terminating application...")
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                p.kill()
            break

        elapsed = time.monotonic() - start_time

        # Normal clean exit
        if exit_code == 0:
            logger.info("Furnace application exited cleanly (code 0). Exiting supervisor.")
            sys.exit(0)

        # Detect Segfault (POSIX signal -11 or shell exit code 139 = 128 + 11)
        is_segfault = (exit_code == -11 or exit_code == 139)
        crash_desc = "Segmentation fault (core dumped)" if is_segfault else f"exit code {exit_code}"

        logger.error(
            "====================================================================\n"
            "[CRITICAL] Furnace application died unexpectedly with %s after %.1fs!\n"
            "Auto-restarting application by '%s'...\n"
            "====================================================================",
            crash_desc, elapsed, " ".join(cmd),
        )

        if elapsed < 2.0:
            consecutive_fast_crashes += 1
            delay = min(5.0, 1.0 * consecutive_fast_crashes)
            time.sleep(delay)
        else:
            consecutive_fast_crashes = 0
            time.sleep(1.0)


if __name__ == "__main__":
    import sys

    # If already running as child worker, or auto-restart is explicitly disabled via env/flag
    if (
        os.environ.get("FURNACE_SUPERVISOR_CHILD") == "1"
        or os.environ.get("FURNACE_NO_SUPERVISOR") == "1"
        or "--no-supervisor" in sys.argv
    ):
        _startup()
        logger.info("Starting Flask on %s:%s", config.FLASK_HOST, config.FLASK_PORT)
        app.run(
            host=config.FLASK_HOST,
            port=config.FLASK_PORT,
            debug=config.FLASK_DEBUG,
            threaded=True,
            use_reloader=False,
        )
    else:
        _run_supervisor()
