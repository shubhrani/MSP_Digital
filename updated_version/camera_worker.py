"""
Background thread: reads frames from the RTSP stream, runs YOLO detection
(throttled by config.PROCESS_EVERY_N_FRAMES), debounces the results, feeds
confirmed class changes into the HeatStateMachine, and keeps the latest
annotated JPEG frame available for the Flask MJPEG route.
"""

import datetime
import logging
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
    def __init__(self, state_machine, rtsp_url=None):
        super().__init__(daemon=True, name="CameraWorker")
        self.state_machine = state_machine
        self.rtsp_url = rtsp_url or config.RTSP_URL

        self.detector = None
        self.detector_error = None

        self.debouncer = ClassDebouncer(
            confirm_frames=config.DEBOUNCE_FRAMES,
            class_ids=state_machine.RELEVANT_CLASS_NAMES,
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
                cap = cv2.VideoCapture(self.rtsp_url)
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
                    # Matched by class NAME, not numeric ID - see
                    # state_machine.RELEVANT_CLASS_NAMES / CLS_* constants.
                    detected_names = {d["name"] for d in detections}
                    newly_confirmed, _ = self.debouncer.update(detected_names)
                    if newly_confirmed:
                        self.state_machine.process_detections(newly_confirmed, datetime.datetime.now())
                    # Heartbeat for the active-fire watchdog: a debounce
                    # edge only fires once per confirm/lose streak, which
                    # isn't enough to keep a multi-minute watchdog clock
                    # alive while class 9 stays confirmed the whole time -
                    # so this checks its *current* confirmed status, every
                    # processed frame, regardless of whether it's new.
                    if self.debouncer.is_confirmed(CLS_FURNACE_ACTIVEFIRE):
                        self.state_machine.note_activefire_seen(datetime.datetime.now())
                except Exception:
                    logger.exception("Detection failed on this frame, showing raw video for it")

            # Purely time-based transitions (the 15-minute tapping timer,
            # the 1.9-minute tilt -> tapping promotion, the active-fire
            # watchdog) don't depend on any detection, so give them a
            # chance to fire on every frame - this way a heat still closes
            # out (or gets cleaned up) on time even during a stretch with
            # no new class detections at all.
            self.state_machine.check_timeouts(datetime.datetime.now())

            annotated = frame
            if self.detector is not None:
                annotated = draw_detections(frame, self._last_detections,
                                             self.state_machine.RELEVANT_CLASS_NAMES)
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
