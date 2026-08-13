"""
Furnace Monitoring Dashboard - Flask app.

Run with:
    python3 app.py

Then open http://<this-machine-ip>:5000 in a browser.

Nothing on the request path or the camera path ever talks to SQL Server
directly. Two background threads own all of it:

  SqlRecorder  (db.py)      - one long-lived connection, fed by a queue.
                              The state machine drops a dict on that queue
                              and returns immediately, so a slow or dead
                              database can't stall frame processing. Rows
                              that can't be written are spooled to disk and
                              replayed in order when the server comes back.
  SqlBackfill  (db_sync.py) - one-shot at startup: pushes the CSVs already
                              on disk into the database, retrying quietly
                              if it isn't reachable yet.

If SQL is switched off (config.SQL_ENABLED = 0), pyodbc isn't installed,
or the server is unreachable, the app runs exactly as it did before -
CSV only, no errors on screen.
"""

import atexit
import logging

from flask import Flask, Response, jsonify, render_template, send_file, abort

import config
import db
import db_sync
from camera_worker import CameraWorker
from state_machine import HeatStateMachine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("furnace_app")

app = Flask(__name__)

# Constructed here, started in _startup(). Constructing it is cheap and
# does no I/O - the connection is only opened by its own thread.
sql_recorder = db.SqlRecorder()

state_machine = HeatStateMachine(data_dir=config.DATA_DIR, index_csv=config.INDEX_CSV,
                                 recorder=sql_recorder)
camera_worker = CameraWorker(state_machine=state_machine)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


def _mjpeg_generator():
    """Streams each frame to the browser the moment camera_worker publishes
    it (pushed via camera_worker.frames(), which itself yields), rather
    than polling get_jpeg() on a fixed timer - lower latency and no
    redundant re-sends of a frame that hasn't changed."""
    boundary = b"--frame"
    for jpeg in camera_worker.frames():
        yield (boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n")


@app.route("/video_feed")
def video_feed():
    return Response(_mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
def api_status():
    status = state_machine.get_status()
    status["camera_connected"] = camera_worker.is_connected()
    status["model_error"] = camera_worker.detector_error
    return jsonify(status)


@app.route("/api/heats")
def api_heats():
    return jsonify(state_machine.get_heats_history())


@app.route("/api/heats/<heat_id>/csv")
def api_heat_csv(heat_id):
    path = state_machine.get_heat_csv_path(heat_id)
    if not path:
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{heat_id}.csv")


@app.route("/api/sql/status")
def api_sql_status():
    """Is the database keeping up? Reads in-memory counters only - it does
    not touch SQL Server, so it stays fast even when the server is down."""
    return jsonify({"recorder": sql_recorder.status(), "backfill": db_sync.status()})


def _startup():
    """Start the background workers. Order matters only a little: the
    backfill goes first so it (rather than the recorder) creates the
    tables, and the recorder queues rows safely either way."""
    sql_recorder.start()
    atexit.register(sql_recorder.stop)

    db_sync.start_background_backfill(state_machine=state_machine)

    camera_worker.start()


if __name__ == "__main__":
    _startup()
    logger.info("Starting Flask on %s:%s", config.FLASK_HOST, config.FLASK_PORT)
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG,
            threaded=True, use_reloader=False)
