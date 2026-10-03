"""
Unit tests for process detection frame saving (both raw and bounding box)
and developer debug metadata APIs.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from camera_worker import CameraWorker
from state_machine import HeatStateMachine


class TestFramesDebug(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_frames_debug_")
        self.orig_frames_dir = config.FRAMES_DIR
        config.FRAMES_DIR = os.path.join(self.tmp_dir, "frames")

    def tearDown(self):
        config.FRAMES_DIR = self.orig_frames_dir
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_save_both_raw_and_annotated_frames_with_metadata(self):
        sm = MagicMock(spec=HeatStateMachine)
        sm.heat_id = "Heat41_099_20260924_120000"
        sm.furnace_id = "4/1"
        sm.state = "SCRAP_CHARGING"
        sm.tilt_count = 2
        sm.RELEVANT_CLASS_IDS = {20, 24, 21}
        sm.get_status_lines.return_value = ["Heat: Heat41_099", "Stage: Scrap Charging"]
        sm.last_event_desc = "scrap charging started"
        sm.last_activefire_at = None

        temp_monitor = MagicMock()
        temp_monitor.get_latest.return_value = {
            "value": 1585.0,
            "field": "PYRO4/1",
            "is_stale": False,
        }
        sm.temperature_monitor = temp_monitor

        worker = CameraWorker(state_machine=sm, rtsp_url="rtsp://dummy/test", stream_id="stream-1")
        worker.detector = MagicMock()
        worker.detector.names = {20: "magnet_moving_with_scrap"}
        worker._last_detections = [
            {
                "class_id": 20,
                "name": "magnet_moving_with_scrap",
                "confidence": 0.885,
                "box": (100.0, 50.0, 300.0, 400.0),
            }
        ]

        dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)

        req = {
            "heat_id": sm.heat_id,
            "event_name": "scrap_charging_start",
            "timestamp": "2026-09-24T12:00:00",
            "description": "Scrap charging detected via magnet",
            "stage": "SCRAP_CHARGING",
        }

        worker._save_debug_frame(dummy_frame, req)

        heat_dir = os.path.join(config.FRAMES_DIR, sm.heat_id)
        self.assertTrue(os.path.isdir(heat_dir))

        files = os.listdir(heat_dir)
        raw_files = [f for f in files if f.endswith("_raw.jpg")]
        ann_files = [f for f in files if f.endswith("_annotated.jpg")]
        json_files = [f for f in files if f.endswith(".json")]

        self.assertEqual(len(raw_files), 1, "Raw frame without bounding box must be saved")
        self.assertEqual(len(ann_files), 1, "Annotated frame with bounding box must be saved")
        self.assertEqual(len(json_files), 1, "Developer debug metadata JSON must be saved")

        with open(os.path.join(heat_dir, json_files[0]), encoding="utf-8") as f:
            meta = json.load(f)

        self.assertEqual(meta["heat_id"], sm.heat_id)
        self.assertEqual(meta["event_name"], "scrap_charging_start")
        self.assertEqual(meta["filename_raw"], raw_files[0])
        self.assertEqual(meta["filename_annotated"], ann_files[0])
        self.assertEqual(meta["frame_dimensions"]["width"], 640)
        self.assertEqual(meta["frame_dimensions"]["height"], 480)
        self.assertEqual(meta["detections_count"], 1)
        self.assertEqual(meta["detections"][0]["class_id"], 20)
        self.assertEqual(meta["detections"][0]["name"], "magnet_moving_with_scrap")
        self.assertAlmostEqual(meta["detections"][0]["confidence"], 0.885, places=3)
        self.assertEqual(meta["temperature"]["value"], 1585.0)
        self.assertEqual(meta["stream"]["stream_id"], "stream-1")

    def test_flask_frames_api_endpoints(self):
        from app import app

        # Create two simulated heat folders in config.FRAMES_DIR
        heat1 = "Heat41_010_20260924_100000"
        heat2 = "Heat41_011_20260924_110000"
        os.makedirs(os.path.join(config.FRAMES_DIR, heat1), exist_ok=True)
        os.makedirs(os.path.join(config.FRAMES_DIR, heat2), exist_ok=True)

        meta1 = {
            "heat_id": heat1,
            "event_name": "scrap_charging_start",
            "timestamp": "2026-09-24T10:00:00",
            "filename_annotated": "scrap_charging_start_annotated.jpg",
            "filename_raw": "scrap_charging_start_raw.jpg",
            "filename": "scrap_charging_start_annotated.jpg",
            "detections": [],
        }
        with open(os.path.join(config.FRAMES_DIR, heat1, "scrap_charging_start.json"), "w") as f:
            json.dump(meta1, f)

        meta2 = {
            "heat_id": heat2,
            "event_name": "sponge_charging_start",
            "timestamp": "2026-09-24T11:00:00",
            "filename_annotated": "sponge_charging_start_annotated.jpg",
            "filename_raw": "sponge_charging_start_raw.jpg",
            "filename": "sponge_charging_start_annotated.jpg",
            "detections": [{"class_id": 6, "name": "sponge_feeder", "confidence": 0.95}],
        }
        with open(os.path.join(config.FRAMES_DIR, heat2, "sponge_charging_start.json"), "w") as f:
            json.dump(meta2, f)

        client = app.test_client()
        slug = list(config.FURNACE_SLOTS.keys())[0]

        # 1. Test /api/frames/heats
        res = client.get(f"/furnace/{slug}/api/frames/heats")
        self.assertEqual(res.status_code, 200)
        heats_data = res.get_json()
        self.assertTrue(len(heats_data) >= 2)
        heat_ids = [h["heat_id"] for h in heats_data]
        self.assertIn(heat1, heat_ids)
        self.assertIn(heat2, heat_ids)

        # 2. Test /api/frames querying specific past heat
        res_past = client.get(f"/furnace/{slug}/api/frames?heat_id={heat1}")
        self.assertEqual(res_past.status_code, 200)
        frames_past = res_past.get_json()
        self.assertEqual(len(frames_past), 1)
        self.assertEqual(frames_past[0]["heat_id"], heat1)
        self.assertIn("url_raw", frames_past[0])
        self.assertIn("url_annotated", frames_past[0])
        self.assertTrue(frames_past[0]["url_raw"].endswith("_raw.jpg"))
        self.assertTrue(frames_past[0]["url_annotated"].endswith("_annotated.jpg"))

        # 3. Test /api/frames with heat_id=all
        res_all = client.get(f"/furnace/{slug}/api/frames?heat_id=all")
        self.assertEqual(res_all.status_code, 200)
        frames_all = res_all.get_json()
        self.assertGreaterEqual(len(frames_all), 2)


if __name__ == "__main__":
    unittest.main()
