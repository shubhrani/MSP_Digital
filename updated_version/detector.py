"""
Thin wrapper around the Ultralytics YOLO model plus drawing helpers for the
video overlay. Kept separate from camera_worker.py so the inference call
and the annotation/drawing code can each be tested or swapped independently.
"""

import cv2

import config

# Colors are BGR (OpenCV convention).
COLOR_OF_INTEREST = (0, 165, 255)   # amber - classes the state machine watches
COLOR_OTHER = (140, 140, 140)       # muted gray - everything else
COLOR_TEXT_BG = (15, 17, 20)


class FurnaceDetector:
    def __init__(self, model_path=None, conf_threshold=None):
        # Imported lazily so the rest of the app can be imported/tested
        # without ultralytics installed.
        from ultralytics import YOLO

        self.model_path = model_path or config.MODEL_PATH
        self.conf_threshold = conf_threshold if conf_threshold is not None else config.CONFIDENCE_THRESHOLD
        self.model = YOLO(self.model_path)

        # Prefer the names embedded in the weights file; fall back to the
        # config-provided map if a model exposes nothing useful.
        model_names = getattr(self.model, "names", None) or {}
        self.names = {int(k): v for k, v in model_names.items()} if model_names else dict(config.CLASS_NAMES)

    def detect(self, frame):
        """Run inference on a single BGR frame. Returns a list of dicts:
        {class_id, name, confidence, box: (x1, y1, x2, y2)}"""
        results = self.model.predict(frame, conf=self.conf_threshold, verbose=False)[0]
        detections = []
        boxes = getattr(results, "boxes", None)
        if boxes is None:
            return detections

        for box in boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0].tolist()]
            detections.append({
                "class_id": cls_id,
                "name": self.names.get(cls_id, str(cls_id)),
                "confidence": conf,
                "box": (x1, y1, x2, y2),
            })
        return detections


# Annotation sizing. Bumped up from the original 0.5/1px so labels and the
# status panel stay readable at normal viewing distance / on a phone.
BOX_LABEL_FONT_SCALE = 0.75
BOX_LABEL_THICKNESS = 2
STATUS_FONT_SCALE = 0.85
STATUS_THICKNESS = 2


def draw_detections(frame, detections, highlight_names):
    """Draw bounding boxes for every detection, highlighting the classes the
    state machine actually cares about (matched by name - see
    state_machine.RELEVANT_CLASS_NAMES)."""
    for det in detections:
        x1, y1, x2, y2 = [int(v) for v in det["box"]]
        is_relevant = det["name"] in highlight_names
        color = COLOR_OF_INTEREST if is_relevant else COLOR_OTHER
        box_thickness = 3 if is_relevant else 2

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, box_thickness)
        label = f"{det['name']} {det['confidence']:.2f}"
        (tw, th), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, BOX_LABEL_FONT_SCALE, BOX_LABEL_THICKNESS)
        cv2.rectangle(frame, (x1, max(0, y1 - th - baseline - 10)),
                      (x1 + tw + 10, y1), color, -1)
        cv2.putText(frame, label, (x1 + 5, max(th + 2, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, BOX_LABEL_FONT_SCALE, (15, 17, 20),
                    BOX_LABEL_THICKNESS, cv2.LINE_AA)
    return frame


def draw_status_overlay(frame, lines):
    """Draw a small status panel (heat id / stage / tilt count) in the
    top-left corner of the frame."""
    if not lines:
        return frame

    pad = 14
    line_h = 34
    box_w = max(cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, STATUS_FONT_SCALE,
                                 STATUS_THICKNESS)[0][0] for line in lines) + pad * 2
    box_h = line_h * len(lines) + pad

    overlay = frame.copy()
    cv2.rectangle(overlay, (8, 8), (8 + box_w, 8 + box_h), COLOR_TEXT_BG, -1)
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

    for i, line in enumerate(lines):
        y = 8 + pad + (i + 1) * line_h - 12
        cv2.putText(frame, line, (8 + pad, y), cv2.FONT_HERSHEY_SIMPLEX,
                    STATUS_FONT_SCALE, (237, 239, 242), STATUS_THICKNESS, cv2.LINE_AA)
    return frame
