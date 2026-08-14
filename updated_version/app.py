"""
Furnace Monitoring Dashboard - Flask app.

Run with:
    python3 app.py

Then open http://<this-machine-ip>:8000 in a browser. "/" redirects to the
default furnace; both furnaces have their own page:

    http://<this-machine-ip>:8000/furnace/4-1
    http://<this-machine-ip>:8000/furnace/4-2

Each furnace gets its own CameraWorker (reads its RTSP stream, runs YOLO,
tracks the heat state machine) and its own CSV files
(see config.FURNACES). Nothing on the request path or either camera path
ever talks to SQL Server directly. Two background thread *kinds* own all
of it:

  SqlRecorder  (db.py)      - ONE long-lived connection shared by both
                              furnaces, fed by a queue. The state machine
                              drops a dict on that queue (tagged with which
                              furnace it's for) and returns immediately, so
                              a slow or dead database can't stall frame
                              processing. Rows that can't be written are
                              spooled to disk and replayed in order when
                              the server comes back.
  SqlBackfill  (db_sync.py) - one per furnace, one-shot at startup: pushes
                              that furnace's CSVs already on disk into the
                              database, retrying quietly if it isn't
                              reachable yet.

If SQL is switched off (config.SQL_ENABLED = 0), pyodbc isn't installed,
or the server is unreachable, the app runs exactly as it did before -
CSV only, no errors on screen.
"""

import atexit
import logging

from flask import Flask, Response, jsonify, redirect, render_template, send_file, url_for, abort

import config
import db
import db_sync
from camera_worker import CameraWorker
from state_machine import HeatStateMachine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("furnace_app")

app = Flask(__name__)

# One connection/queue shared by every furnace - each row it writes carries
# its own FurnaceId, so one recorder is all that's needed no matter how many
# furnaces this process is watching.
sql_recorder = db.SqlRecorder()


class FurnaceRuntime:
    """Everything needed to run and expose one furnace's dashboard: its
    state machine (owns that furnace's CSVs + pushes to the shared
    recorder) and its camera worker (owns that furnace's RTSP stream)."""

    def __init__(self, slug, cfg):
        self.slug = slug
        self.furnace_id = cfg["furnace_id"]
        self.name = cfg["name"]
        self.rtsp_url = cfg["rtsp_url"]

        self.state_machine = HeatStateMachine(
            data_dir=cfg["data_dir"],
            index_csv=cfg["index_csv"],
            recorder=sql_recorder,
            furnace_id=self.furnace_id,
            heat_id_prefix=cfg["heat_id_prefix"],
        )
        self.camera_worker = CameraWorker(
            state_machine=self.state_machine,
            rtsp_url=self.rtsp_url,
        )


# Preserves config.FURNACES's own ordering (dict, Python 3.7+) so the nav
# bar / home page list furnaces in a stable, predictable order.
FURNACES = {slug: FurnaceRuntime(slug, cfg) for slug, cfg in config.FURNACES.items()}
NAV_FURNACES = [{"slug": f.slug, "name": f.name} for f in FURNACES.values()]


def _get_furnace_or_404(slug):
    furnace = FURNACES.get(slug)
    if furnace is None:
        abort(404)
    return furnace


@app.route("/")
def home():
    if config.DEFAULT_FURNACE_SLUG in FURNACES:
        return redirect(url_for("dashboard", slug=config.DEFAULT_FURNACE_SLUG))
    return render_template("home.html", furnaces=NAV_FURNACES)


@app.route("/furnace/<slug>")
def dashboard(slug):
    furnace = _get_furnace_or_404(slug)
    return render_template(
        "dashboard.html",
        furnace_slug=furnace.slug,
        furnace_name=furnace.name,
        furnace_id=furnace.furnace_id,
        nav_furnaces=NAV_FURNACES,
    )


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
    furnace = _get_furnace_or_404(slug)
    return Response(_mjpeg_generator(furnace.camera_worker),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/furnace/<slug>/api/status")
def api_status(slug):
    furnace = _get_furnace_or_404(slug)
    status = furnace.state_machine.get_status()
    status["camera_connected"] = furnace.camera_worker.is_connected()
    status["model_error"] = furnace.camera_worker.detector_error
    status["furnace_id"] = furnace.furnace_id
    status["furnace_name"] = furnace.name
    return jsonify(status)


@app.route("/furnace/<slug>/api/heats")
def api_heats(slug):
    furnace = _get_furnace_or_404(slug)
    return jsonify(furnace.state_machine.get_heats_history())


@app.route("/furnace/<slug>/api/heats/<heat_id>/csv")
def api_heat_csv(slug, heat_id):
    furnace = _get_furnace_or_404(slug)
    path = furnace.state_machine.get_heat_csv_path(heat_id)
    if not path:
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{heat_id}.csv")


@app.route("/furnace/<slug>/api/sql/status")
def api_sql_status(slug):
    """Is the database keeping up? Reads in-memory counters only - it does
    not touch SQL Server, so it stays fast even when the server is down."""
    furnace = _get_furnace_or_404(slug)
    return jsonify({"recorder": sql_recorder.status(), "backfill": db_sync.status(furnace.slug)})


def _startup():
    """Start the background workers. Order matters only a little: each
    furnace's backfill goes first so it (rather than the recorder) creates
    the shared tables, and the recorder queues rows safely either way."""
    sql_recorder.start()
    atexit.register(sql_recorder.stop)

    for furnace in FURNACES.values():
        db_sync.start_background_backfill(
            furnace.slug,
            data_dir=furnace.state_machine.data_dir,
            index_csv=furnace.state_machine.index_csv,
            furnace_id=furnace.furnace_id,
            state_machine=furnace.state_machine,
        )
        furnace.camera_worker.start()
        logger.info("Started furnace %s (%s) - camera: %s",
                    furnace.slug, furnace.furnace_id, furnace.rtsp_url)


if __name__ == "__main__":
    _startup()
    logger.info("Starting Flask on %s:%s", config.FLASK_HOST, config.FLASK_PORT)
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG,
            threaded=True, use_reloader=False)
