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

import cv2

import config
from debounce import ClassDebouncer
from detector import FurnaceDetector, draw_detections, draw_status_overlay
from state_machine import CLS_FURNACE_ACTIVEFIRE

logger = logging.getLogger(__name__)

# A simple placeholder frame used whenever the stream is unavailable, so the
# <img> tag in the browser always has something valid to show.
_PLACEHOLDER_TEXT = "Waiting for camera..."


class CameraWorker(threading.Thread):
    def __init__(self, state_machine, rtsp_url=None, stream_id=None):
        super().__init__(daemon=True, name="CameraWorker")
        self.state_machine = state_machine
        self.rtsp_url = rtsp_url or config.RTSP_URL
        self.stream_id = stream_id

        self.detector = None
        self.detector_error = None

        self.debouncer = ClassDebouncer(
            confirm_frames=config.DEBOUNCE_FRAMES,
            class_ids=state_machine.RELEVANT_CLASS_IDS,
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

    # -----------------------------------------------------------------
    def run(self):
        try:
            self.detector = FurnaceDetector()
        except Exception as exc:  # pragma: no cover - depends on environment
            logger.exception("Could not load model, video feed will show raw stream only")
            self.detector_error = str(exc)

        cap = None
        while self._running:
            if cap is None or not cap.isOpened():
                logger.info("Connecting to RTSP stream: %s", self.rtsp_url)
                cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000)   # 10s to establish
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)    # 5s max wait per frame
                if not cap.isOpened():
                    self._connected = False
                    self._set_placeholder_frame("Cannot connect to RTSP stream")
                    time.sleep(config.RECONNECT_DELAY_SECONDS)
                    continue
                self._connected = True

            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning("Lost the RTSP stream, reconnecting...")
                self._connected = False
                cap.release()
                cap = None
                self._set_placeholder_frame("Stream interrupted, reconnecting...")
                time.sleep(config.RECONNECT_DELAY_SECONDS)
                continue

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
                    if newly_confirmed:
                        self.state_machine.process_detections(newly_confirmed, datetime.datetime.now())
                    # Heartbeat for the active-fire watchdog: a debounce
                    # edge only fires once per confirm/lose streak, which
                    # isn't enough to keep a multi-minute watchdog clock
                    # alive while class 3 stays confirmed the whole time -
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
