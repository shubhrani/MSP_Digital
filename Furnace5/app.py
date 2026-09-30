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

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for, abort

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


def _placeholder_generator(message="Waiting for camera stream..."):
    import cv2
    import numpy as np
    import time
    frame = np.zeros((480, 854, 3), dtype="uint8")
    frame[:] = (17, 19, 23)
    cv2.putText(frame, message, (40, 240), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (138, 147, 160), 2, cv2.LINE_AA)
    ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY])
    if ok:
        boundary = b"--frame"
        body = boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n"
        while True:
            yield body
            time.sleep(1.0)


def _mjpeg_generator(camera_worker):
    """Streams each frame to the browser the moment camera_worker publishes
    it (pushed via camera_worker.frames(), which itself yields), rather
    than polling get_jpeg() on a fixed timer - lower latency and no
    redundant re-sends of a frame that hasn't changed."""
    boundary = b"--frame"
    for jpeg in camera_worker.frames():
        yield (boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")


@app.route("/furnace/<slug>/video_feed")
def video_feed(slug):
    slot = _get_slot_or_404(slug)
    if not slot.camera_worker:
        return Response(_placeholder_generator(f"{slot.name} - Waiting for camera..."),
                        mimetype="multipart/x-mixed-replace; boundary=frame")
    return Response(_mjpeg_generator(slot.camera_worker),
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


if __name__ == "__main__":
    _startup()
    logger.info("Starting Flask on %s:%s", config.FLASK_HOST, config.FLASK_PORT)
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG,
            threaded=True, use_reloader=False)
