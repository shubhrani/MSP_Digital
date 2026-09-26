import cv2
from flask import Flask, Response, render_template_string
from ultralytics import YOLO

app = Flask(__name__)

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

def generate_frames():
    cap = cv2.VideoCapture(VIDEO_PATH)

    if not cap.isOpened():
        print(f"Could not open video: {VIDEO_PATH}")
        return

    while True:
        ret, frame = cap.read()

        if not ret:
            # Loop video
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue
            
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

        # Encode frame as JPEG
        ret, buffer = cv2.imencode('.jpg', annotated_frame)
        if not ret:
            continue
        
        frame_bytes = buffer.tobytes()

        # Yield for multipart stream
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

@app.route('/')
def index():
    # Simple HTML template to display the video stream
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>YOLOv8 Live Detection</title>
        <style>
            body { font-family: sans-serif; text-align: center; background-color: #f0f2f5; margin: 0; padding: 20px; }
            h1 { color: #333; }
            .video-container { margin-top: 20px; background: #000; padding: 10px; border-radius: 8px; display: inline-block; box-shadow: 0 4px 8px rgba(0,0,0,0.2); }
            img { border-radius: 4px; max-width: 100%; height: auto; }
        </style>
    </head>
    <body>
        <h1>Furnace Detection Feed</h1>
        <div class="video-container">
            <img src="{{ url_for('video_feed') }}" width="1220" height="720" alt="Video Feed">
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/video_feed')
def video_feed():
    # Returns the multipart HTTP response
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
