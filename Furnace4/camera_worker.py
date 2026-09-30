"""
Background thread: reads frames from the RTSP stream, runs YOLO detection
(throttled by config.PROCESS_EVERY_N_FRAMES), debounces the results, feeds
confirmed class changes into the HeatStateMachine, and keeps the latest
annotated JPEG frame available for the Flask MJPEG route.
"""

import datetime
import json
import logging
import os
import threading
import time

# Force RTSP over TCP and a sane socket timeout for every OpenCV VideoCapture.
# UDP is the default in some builds; on plant LANs it drops packets and
# cap.read() returns False, which your loop then treats as a disconnect.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000|max_delay;500000",
)

import cv2

import config
from debounce import ClassDebouncer
from detector import FurnaceDetector, draw_detections, draw_status_overlay
from state_machine import (
    CLS_FURNACE_ACTIVEFIRE,
    CLS_FURNACE_NORMAL,
    CLS_NEGATIVE_FAN,
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
    CLS_COIL_POSITION_PLACEDNEARFURNACE,
    CLS_FURMA_POSITION_PLACEDNEARFURNACE,
    CLS_FURNACE_OPERATION_FURMAREPLACED,
)

logger = logging.getLogger(__name__)

# A simple placeholder frame used whenever the stream is unavailable, so the
# <img> tag in the browser always has something valid to show.
_PLACEHOLDER_TEXT = "Waiting for camera..."

# ---------------------------------------------------------------------------
# Maintenance detection
# ---------------------------------------------------------------------------

MAINTENANCE_DIRECT_CLASS_IDS = {
    CLS_FURNACE_OPERATION_UNDERMAINTENANCE,  # 28
    CLS_COIL_POSITION_PLACEDNEARFURNACE,     # 0
    CLS_FURMA_POSITION_PLACEDNEARFURNACE,    # 39
    CLS_FURNACE_OPERATION_FURMAREPLACED,     # 31
}

# Also track:
#   class 1  = furnace position normal
#   class 9  = negative fan
#
# Class 9 means maintenance ONLY when the fan is physically detected
# inside the bounding box of class 1.
MAINTENANCE_TRACKED_CLASS_IDS = (
    MAINTENANCE_DIRECT_CLASS_IDS
    | {
        CLS_FURNACE_NORMAL,
        CLS_NEGATIVE_FAN,
    }
)


def _box_center(box):
    x1, y1, x2, y2 = box
    return (
        (float(x1) + float(x2)) / 2.0,
        (float(y1) + float(y2)) / 2.0,
    )


def _point_inside_box(point, box):
    px, py = point
    x1, y1, x2, y2 = [float(v) for v in box]

    return (
        x1 <= px <= x2
        and y1 <= py <= y2
    )


def _negative_fan_inside_normal_box(detections):
    """
    Maintenance condition:

        CLS_NEGATIVE_FAN (9)

    must be spatially inside:

        CLS_FURNACE_NORMAL (1)

    We use the CENTER of the fan bounding box. This prevents a tiny
    bounding-box overlap from falsely declaring the furnace under
    maintenance.
    """

    normal_boxes = [
        d["box"]
        for d in detections
        if d.get("class_id") == CLS_FURNACE_NORMAL
        and d.get("box") is not None
    ]

    fan_boxes = [
        d["box"]
        for d in detections
        if d.get("class_id") == CLS_NEGATIVE_FAN
        and d.get("box") is not None
    ]

    if not normal_boxes or not fan_boxes:
        return False

    for fan_box in fan_boxes:
        fan_center = _box_center(fan_box)

        for normal_box in normal_boxes:
            if _point_inside_box(fan_center, normal_box):
                return True

    return False


def maintenance_reason(detections, debouncer, newly_confirmed):
    """
    Furnace is considered under maintenance when:

      1. Any direct maintenance class is confirmed:
           28 - furnace_operation_undermaintenance
            0 - coil_position_placednearfurnace
           39 - furma_position_placednearfurnace
           31 - furnace_operation_furmareplaced

      OR

      2. Class 9 (negative fan) is confirmed AND its bounding-box
         centre is inside class 1 (furnace normal) bounding box.
    """

    # ---------------------------------------------------------------
    # Direct maintenance classes
    # ---------------------------------------------------------------
    direct = (
        set(newly_confirmed)
        & MAINTENANCE_DIRECT_CLASS_IDS
    )

    if direct:
        class_id = sorted(direct)[0]
        return f"maintenance class {class_id} confirmed"

    # ---------------------------------------------------------------
    # Negative fan inside normal furnace box
    # ---------------------------------------------------------------
    fan_confirmed = debouncer.is_confirmed(
        CLS_NEGATIVE_FAN
    )

    normal_confirmed = debouncer.is_confirmed(
        CLS_FURNACE_NORMAL
    )

    if (
        fan_confirmed
        and normal_confirmed
        and _negative_fan_inside_normal_box(detections)
    ):
        return (
            "maintenance: negative fan detected "
            "inside furnace normal-position box"
        )

    return None


class CameraWorker(threading.Thread):
    def __init__(
        self,
        state_machine,
        rtsp_url=None,
        stream_id=None,
        on_maintenance_detected=None,
    ):
        super().__init__(daemon=True, name="CameraWorker")
        self.state_machine = state_machine
        self.rtsp_url = rtsp_url or config.RTSP_URL
        self.stream_id = stream_id
        self.on_maintenance_detected = on_maintenance_detected
        self._maintenance_reported = False

        self.detector = None
        self.detector_error = None

        self.debouncer = ClassDebouncer(
            confirm_frames=config.DEBOUNCE_FRAMES,
            class_ids=state_machine.RELEVANT_CLASS_IDS,
        )

        self.maintenance_debouncer = ClassDebouncer(
            confirm_frames=config.DEBOUNCE_FRAMES,
            class_ids=MAINTENANCE_TRACKED_CLASS_IDS,
        )

        # A Condition (rather than a plain Lock + polling) lets frames()
        # push each new frame out to viewers the instant it's ready,
        # instead of viewers waking up on a fixed timer and re-checking.
        self._frame_condition = threading.Condition()
        self._latest_jpeg = None
        self._frame_seq = 0
        self._connected = False
        self._running = True
        self._frame_count = 0
        self._last_detections = []
        self._consecutive_read_failures = 0

    # -----------------------------------------------------------------
    def _open_rtsp_capture(self):
        """
        Open the assigned RTSP camera.

        Returns an opened VideoCapture or None.

        CameraWorker.run() will keep calling this method forever while the
        worker is running, so a temporary camera/network failure automatically
        recovers when the stream comes back.
        """

        cap = None

        try:
            # Newer OpenCV builds support open/read timeout parameters directly
            # in the constructor. This is preferable because OPEN_TIMEOUT must
            # apply while the connection is actually being established.
            cap = cv2.VideoCapture(
                self.rtsp_url,
                cv2.CAP_FFMPEG,
                [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                    10000,

                    cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                    5000,
                ],
            )

        except (TypeError, cv2.error):
            # Compatibility fallback for older OpenCV versions.
            cap = cv2.VideoCapture(
                self.rtsp_url,
                cv2.CAP_FFMPEG,
            )

            try:
                cap.set(
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                    5000,
                )
            except Exception:
                pass

        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()

            return None

        # Keep latency down. Some FFmpeg/OpenCV builds ignore this, which is OK.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        self._consecutive_read_failures = 0
        return cap

    def run(self):
        try:
            self.detector = FurnaceDetector()
        except Exception as exc:  # pragma: no cover - depends on environment
            logger.exception("Could not load model, video feed will show raw stream only")
            self.detector_error = str(exc)

        cap = None
        while self._running:
            if cap is None or not cap.isOpened():
                logger.info(
                    "Connecting to RTSP stream %s: %s",
                    self.stream_id,
                    self.rtsp_url,
                )

                cap = self._open_rtsp_capture()

                if cap is None:
                    self._connected = False

                    self._set_placeholder_frame(
                        "Camera offline - reconnecting..."
                    )

                    if self.state_machine is not None:
                        self.state_machine.note_camera_state(False)

                    logger.warning(
                        "RTSP connection failed for %s. "
                        "Retrying in %s seconds.",
                        self.stream_id,
                        config.RECONNECT_DELAY_SECONDS,
                    )

                    time.sleep(
                        config.RECONNECT_DELAY_SECONDS
                    )

                    continue

                self._connected = True
                if self.state_machine is not None:
                    self.state_machine.note_camera_state(True)

                logger.info(
                    "RTSP connected: %s",
                    self.stream_id,
                )

            # ---------------------------------------------------------------
            # Read camera frame
            # Drain any frames FFmpeg has buffered while we were running detection,
            # so we always work on the freshest one. grab() is cheap (no decode);
            # retrieve() decodes only the last grabbed frame.
            # ---------------------------------------------------------------
            if cap is not None and cap.isOpened():
                # Discard up to 4 stale frames; if the camera is idle, this loop
                # exits immediately because grab() returns False.
                for _ in range(4):
                    if not cap.grab():
                        break
                ret, frame = cap.retrieve()
            else:
                ret, frame = False, None

            if not ret or frame is None:
                self._consecutive_read_failures += 1

                # A single dropped frame is normal on RTSP. Only tear down and
                # reconnect after several in a row, so transient glitches don't
                # cause a connect/disconnect loop.
                if self._consecutive_read_failures < 10:
                    time.sleep(0.05)
                    continue

                logger.warning(
                    "RTSP stream %s interrupted (%d consecutive failed reads). Reconnecting...",
                    self.stream_id, self._consecutive_read_failures,
                )
                self._consecutive_read_failures = 0
                self._connected = False
                if self.state_machine is not None:
                    self.state_machine.note_camera_state(False)
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
                self._set_placeholder_frame("Camera interrupted - reconnecting...")
                time.sleep(config.RECONNECT_DELAY_SECONDS)
                continue

            # Read succeeded -> reset the failure counter.
            self._consecutive_read_failures = 0

            self._frame_count += 1
            should_run_detection = (
                self.detector is not None
                and self._frame_count % max(1, config.PROCESS_EVERY_N_FRAMES) == 0
            )

            if should_run_detection:
                try:
                    detections = self.detector.detect(frame)
                    self._last_detections = detections

                    detected_ids = {d["class_id"] for d in detections}

                    newly_confirmed, _ = self.debouncer.update(detected_ids)

                    maintenance_newly_confirmed, _ = (
                        self.maintenance_debouncer.update(detected_ids)
                    )

                    if newly_confirmed:
                        self.state_machine.process_detections(
                            newly_confirmed,
                            datetime.datetime.now(),
                        )

                    if not self._maintenance_reported:
                        reason = maintenance_reason(
                            detections,
                            self.maintenance_debouncer,
                            maintenance_newly_confirmed,
                        )

                        if reason is not None:
                            self._maintenance_reported = True

                            if self.on_maintenance_detected is not None:
                                self.on_maintenance_detected(reason)
                    # Heartbeat for the active-fire watchdog: a debounce
                    # edge only fires once per confirm/lose streak, which
                    # isn't enough to keep a multi-minute watchdog clock
                    # alive while class 5 stays confirmed the whole time -
                    # so this checks its *current* confirmed status, every
                    # processed frame, regardless of whether it's new.
                    if self.debouncer.is_confirmed(CLS_FURNACE_ACTIVEFIRE):
                        self.state_machine.note_activefire_seen(datetime.datetime.now())
                except Exception:
                    logger.exception("Detection failed on this frame, showing raw video for it")

            # Purely time/continuous transitions such as the 45-minute scrap cap,
            # continuous PYRO movement check, 15-minute tapping cap, and
            # active-fire watchdog are evaluated every frame.
            self.state_machine.check_timeouts(datetime.datetime.now())

            # Save any debug frames the state machine queued on this or an
            # earlier frame (transition evidence).
            self._drain_frame_requests(frame)

            annotated = frame
            if self.detector is not None:
                annotated = draw_detections(frame, self._last_detections,
                                             self.state_machine.RELEVANT_CLASS_IDS)
            annotated = draw_status_overlay(annotated, self.state_machine.get_status_lines())

            ok, jpeg = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY])
            if ok:
                self._publish_frame(jpeg.tobytes())

        if cap is not None:
            cap.release()
        # Wake up any generators still waiting so they can notice
        # self._running is now False and exit cleanly.
        with self._frame_condition:
            self._frame_condition.notify_all()

    # -----------------------------------------------------------------
    def _set_placeholder_frame(self, message):
        import numpy as np

        frame = np.zeros((480, 854, 3), dtype="uint8")
        frame[:] = (17, 19, 23)
        cv2.putText(frame, message, (40, 240), cv2.FONT_HERSHEY_SIMPLEX,
                    0.9, (138, 147, 160), 2, cv2.LINE_AA)
        ok, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY])
        if ok:
            self._publish_frame(jpeg.tobytes())

    def _publish_frame(self, jpeg_bytes):
        with self._frame_condition:
            self._latest_jpeg = jpeg_bytes
            self._frame_seq += 1
            self._frame_condition.notify_all()

    def _drain_frame_requests(self, frame):
        """Pull every pending frame-capture request from the state machine
        and save the current (raw, pre-overlay) frame for each one."""
        try:
            requests = self.state_machine.get_pending_frame_requests()
        except Exception:
            logger.exception("Could not read pending frame requests")
            return
        for req in requests:
            try:
                self._save_debug_frame(frame, req)
            except Exception:
                logger.exception("Failed to save debug frame for %s",
                                 req.get("event_name"))

    def _save_debug_frame(self, frame, req):
        heat_id = req.get("heat_id") or f"unassigned_{self.state_machine.furnace_id or 'furnace'}"
        event_name = req.get("event_name") or "event"
        ts_iso = req.get("timestamp") or datetime.datetime.now().isoformat()

        safe_ts = (ts_iso.replace(":", "").replace("-", "")
                          .replace(".", "").replace("+", "_")[:17])

        heat_dir = os.path.join(config.FRAMES_DIR, heat_id)
        os.makedirs(heat_dir, exist_ok=True)

        # 1. Raw frame (without bounding boxes or status overlay)
        raw_frame = frame.copy()
        raw_filename = f"{event_name}_{safe_ts}_raw.jpg"
        raw_filepath = os.path.join(heat_dir, raw_filename)

        # 2. Annotated frame (with bounding boxes, labels, and status overlay)
        annotated_frame = frame.copy()
        if self.detector is not None and self._last_detections:
            annotated_frame = draw_detections(
                annotated_frame,
                self._last_detections,
                self.state_machine.RELEVANT_CLASS_IDS,
            )
        annotated_frame = draw_status_overlay(
            annotated_frame,
            self.state_machine.get_status_lines(),
        )
        annotated_filename = f"{event_name}_{safe_ts}_annotated.jpg"
        annotated_filepath = os.path.join(heat_dir, annotated_filename)

        # Legacy alias for backward compatibility
        legacy_filename = f"{event_name}_{safe_ts}.jpg"
        legacy_filepath = os.path.join(heat_dir, legacy_filename)

        ok_raw = cv2.imwrite(raw_filepath, raw_frame,
                             [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY])
        ok_ann = cv2.imwrite(annotated_filepath, annotated_frame,
                             [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY])
        # Also write the legacy filename so any older endpoints or links still work
        cv2.imwrite(legacy_filepath, annotated_frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY])

        if not ok_raw or not ok_ann:
            logger.warning("cv2.imwrite failed when saving debug frames for %s in %s",
                           event_name, heat_dir)

        # 3. Gather comprehensive developer debug metadata
        h, w = frame.shape[:2]
        c = frame.shape[2] if len(frame.shape) > 2 else 1

        formatted_detections = []
        for d in (self._last_detections or []):
            try:
                box = [round(float(v), 1) for v in d.get("box", (0, 0, 0, 0))]
                x1, y1, x2, y2 = box
                cid = int(d.get("class_id", -1))
                conf = float(d.get("confidence", 0.0))
                formatted_detections.append({
                    "class_id": cid,
                    "name": str(d.get("name", "")),
                    "confidence": round(conf, 4),
                    "confidence_pct": f"{conf * 100:.1f}%",
                    "box": box,
                    "width": round(x2 - x1, 1),
                    "height": round(y2 - y1, 1),
                    "area": round((x2 - x1) * (y2 - y1), 1),
                    "is_relevant": cid in self.state_machine.RELEVANT_CLASS_IDS,
                })
            except Exception:
                continue

        confirmed_classes = []
        if hasattr(self.debouncer, "_confirmed"):
            names_map = getattr(self.detector, "names", {}) if self.detector else {}
            for cid in self.debouncer._confirmed:
                confirmed_classes.append({
                    "class_id": int(cid),
                    "name": names_map.get(cid, str(cid)),
                })

        temp_data = None
        if getattr(self.state_machine, "temperature_monitor", None):
            try:
                temp_data = self.state_machine.temperature_monitor.get_latest()
            except Exception:
                pass

        meta = {
            "heat_id": heat_id,
            "furnace_id": getattr(self.state_machine, "furnace_id", None),
            "event_name": event_name,
            "timestamp": ts_iso,
            "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "stage": req.get("stage") or getattr(self.state_machine, "state", "UNKNOWN"),
            "description": req.get("description", ""),
            "filename": annotated_filename,
            "filename_annotated": annotated_filename,
            "filename_raw": raw_filename,
            "frame_dimensions": {
                "width": int(w),
                "height": int(h),
                "channels": int(c),
            },
            "detections_count": len(formatted_detections),
            "detections": formatted_detections,
            "confirmed_classes": confirmed_classes,
            "tilt_count": getattr(self.state_machine, "tilt_count", 0),
            "temperature": temp_data,
            "stream": {
                "stream_id": self.stream_id,
                "rtsp_url": self.rtsp_url,
            },
            "state_machine": {
                "state": getattr(self.state_machine, "state", ""),
                "last_event": getattr(self.state_machine, "last_event_desc", ""),
                "last_activefire_at": (
                    self.state_machine.last_activefire_at.isoformat()
                    if getattr(self.state_machine, "last_activefire_at", None) else None
                ),
            },
        }

        json_path = os.path.join(heat_dir, f"{event_name}_{safe_ts}.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except OSError:
            logger.exception("Failed to write metadata JSON for %s", json_path)

        logger.info(
            "Saved debug frames for %s (heat %s): raw=%s, annotated=%s, detections=%d",
            event_name, heat_id, raw_filename, annotated_filename, len(formatted_detections)
        )

    def frames(self):
        """Generator that yields each newly-annotated JPEG frame as soon as
        it's published, instead of the caller polling on a fixed timer.
        Safe to call from multiple browser tabs at once - each call gets
        its own generator, all waiting on the same condition."""
        last_seq = 0
        while self._running:
            with self._frame_condition:
                # Wake up as soon as a new frame lands; the 1s timeout is
                # just so this loop can also notice self._running flipping
                # to False and exit even with no camera activity.
                self._frame_condition.wait_for(
                    lambda: self._frame_seq != last_seq or not self._running,
                    timeout=1.0,
                )
                jpeg_bytes = self._latest_jpeg
                last_seq = self._frame_seq
            if jpeg_bytes is not None:
                yield jpeg_bytes

    def get_jpeg(self):
        """Kept for callers that just want the single latest frame (e.g. a
        one-off snapshot) rather than the streaming generator above."""
        with self._frame_condition:
            return self._latest_jpeg

    def is_connected(self):
        return self._connected

    def stop(self):
        self._running = False
        with self._frame_condition:
            self._frame_condition.notify_all()
