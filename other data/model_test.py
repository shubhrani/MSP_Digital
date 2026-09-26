from ultralytics import YOLO
import cv2

# ============================================================
# CONFIGURATION
# ============================================================

MODEL_PATH = r"C:\msptrain\runs\detect\train-4\weights\best.pt"
VIDEO_PATH = r"C:\Users\shubh\Downloads\Furnace_video_for_testing\vlc-record-2026-07-11-11h54m48s-rtsp___10.50.40.152_554_video_live-.mp4"

CONFIDENCE = 0.25
IMG_SIZE = 640
IGNORE_CLASSES = [0, 1, 2, 12, 13, 14, 21, 22, 25]

# ============================================================
# LOAD MODEL
# ============================================================

model = YOLO(MODEL_PATH)

KEEP_CLASSES = [k for k in model.names.keys() if k not in IGNORE_CLASSES]

print("Model classes:")
for class_id, class_name in model.names.items():
    print(f"{class_id}: {class_name}")

# ============================================================
# OPEN VIDEO
# ============================================================

cap = cv2.VideoCapture(VIDEO_PATH)

if not cap.isOpened():
    raise RuntimeError(f"Could not open video: {VIDEO_PATH}")

print("\nStarting detection...")
print("Press Q to stop.")

# ============================================================
# PROCESS VIDEO
# ============================================================

while True:

    ret, frame = cap.read()

    if not ret:
        print("Video ended.")
        break

    # YOLO detection
    results = model.predict(
        source=frame,
        imgsz=IMG_SIZE,
        conf=CONFIDENCE,
        classes=KEEP_CLASSES,
        verbose=False
    )

    # Draw detections
    annotated_frame = results[0].plot()

    # Resize frame
    annotated_frame = cv2.resize(annotated_frame, (1220, 720))

    # Display
    cv2.imshow("YOLO Video Detection", annotated_frame)

    # Press Q to quit
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

# ============================================================
# CLEANUP
# ============================================================

cap.release()
cv2.destroyAllWindows()

print("Detection stopped.")