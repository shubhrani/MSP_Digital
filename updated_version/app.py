"""
Furnace Monitoring Dashboard - Flask app.

Run with:
    python3 app.py

Then open http://<this-machine-ip>:5000 in a browser.
"""

import logging

from flask import Flask, Response, jsonify, render_template, send_file, abort

import config
from camera_worker import CameraWorker
from state_machine import HeatStateMachine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("furnace_app")

app = Flask(__name__)

state_machine = HeatStateMachine(data_dir=config.DATA_DIR, index_csv=config.INDEX_CSV)
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


if __name__ == "__main__":
    camera_worker.start()
    logger.info("Starting Flask on %s:%s", config.FLASK_HOST, config.FLASK_PORT)
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG,
             threaded=True, use_reloader=False)
