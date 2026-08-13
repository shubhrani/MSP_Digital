"""
server.py

FastAPI backend for the furnace dashboard:

  GET  /                  -> dashboard page (static/index.html)
  GET  /video_feed         -> annotated MJPEG stream
  GET  /api/status          -> current process snapshot (JSON, one-shot)
  GET  /api/history          -> completed heats this session (JSON)
  WS   /ws/status              -> current process snapshot, pushed every second

Run with:
  uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import threading
import time
from contextlib import asynccontextmanager

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import inference_loop
from process_tracker import ProcessTracker

RTSP_URL = "rtsp://your-camera-url"  # <-- point this at your camera
MODEL_PATH = r"C:\Users\shubh\Downloads\MSP_Digital_Project_Arnab_sir\pm-sms-custom-model\auto_annot_base-4\weights\best.pt"

tracker: ProcessTracker = inference_loop.tracker
_latest_jpeg: dict = {"bytes": None}


def _frame_sink(annotated_bgr) -> None:
    ok, buf = cv2.imencode(".jpg", annotated_bgr)
    if ok:
        _latest_jpeg["bytes"] = buf.tobytes()


@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(
        target=inference_loop.run,
        args=(RTSP_URL, MODEL_PATH, _frame_sink),
        daemon=True,
    )
    t.start()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")


def _mjpeg_generator():
    boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    while True:
        jpeg = _latest_jpeg["bytes"]
        if jpeg is not None:
            yield boundary + jpeg + b"\r\n"
        time.sleep(0.03)  # ~30 fps cap on the served stream


@app.get("/video_feed")
def video_feed():
    return StreamingResponse(
        _mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/status")
def status():
    return tracker.snapshot()


@app.get("/api/history")
def history():
    return tracker.history()


@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json(tracker.snapshot())
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
