"""
inference_loop.py

Reference integration between best.pt and ProcessTracker. This uses a plain
OpenCV VideoCapture(rtsp_url) as the frame source so it runs standalone -
swap `frame_source()` for your existing GStreamer appsink pull (wherever
your `_annotate()` function currently gets its frames) and keep everything
else. The only integration points ProcessTracker actually needs are:

    cls_name, conf = top_relevant_detection(result, model.names)
    tracker.update(cls_name, conf)

Call those two lines from inside your existing per-frame loop and you're
done - you don't need to adopt this file's video I/O at all.
"""

import time
from typing import Optional, Tuple

import cv2
from ultralytics import YOLO

from process_tracker import ProcessTracker, RELEVANT_CLASSES, TAPPING_CLASS

CONF_THRESHOLD = 0.5

# Only relevant when multiple of the 5 classes fire on the SAME frame (e.g. the
# furnace sits in a normal position while the magnet is also feeding scrap).
# Tapping always wins outright since it ends the heat; after that, an actual
# activity class should win over the "normal" baseline class.
PRIORITY = [
    TAPPING_CLASS,
    "magnet_feeding_scrap",
    "spange_feeder_engaged",
    "furnace_position_tilding",
    "furnace_position_normal",
]

tracker = ProcessTracker()  # import this same instance from server.py


def top_relevant_detection(result, class_names: dict) -> Tuple[Optional[str], float]:
    """Collapse one frame's YOLO boxes down to the single class ProcessTracker
    should see, using PRIORITY to break ties when more than one relevant
    class is detected in the same frame."""
    best_conf_by_class = {}
    for box in result.boxes:
        cls_name = class_names[int(box.cls)]
        conf = float(box.conf)
        if cls_name in RELEVANT_CLASSES and conf >= CONF_THRESHOLD:
            if conf > best_conf_by_class.get(cls_name, 0.0):
                best_conf_by_class[cls_name] = conf

    for cls_name in PRIORITY:
        if cls_name in best_conf_by_class:
            return cls_name, best_conf_by_class[cls_name]
    return None, 0.0


def run(rtsp_url: str, model_path: str = "best.pt", on_frame=None,
        max_calls_per_second: float = 5.0) -> None:
    """
    on_frame: optional callback(annotated_bgr_frame) - e.g. hand this to your
    MJPEG frame sink in server.py.

    max_calls_per_second throttles how often tracker.update() is called
    (independent of how fast frames actually arrive). This matters because
    WINDOW_SIZE=30 in process_tracker.py is defined in "updates", not
    seconds - throttling to a known, steady rate keeps the 30-frame window
    meaning a predictable span of real time regardless of RTSP/GPU jitter.
    """
    model = YOLO(model_path)
    cap = cv2.VideoCapture(rtsp_url)  # swap for your GStreamer appsink source
    min_interval = 1.0 / max_calls_per_second
    last_update = 0.0

    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.1)
            continue

        result = model.predict(frame, verbose=False)[0]

        now = time.monotonic()
        if now - last_update >= min_interval:
            cls_name, conf = top_relevant_detection(result, model.names)
            tracker.update(cls_name, conf)
            last_update = now

        if on_frame is not None:
            annotated = result.plot()  # or your own _annotate() from the GStreamer pipeline
            on_frame(annotated)


if __name__ == "__main__":
    run("rtsp://your-camera-url", "best.pt")
