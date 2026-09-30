"""
Unit tests for furnace_manager.py: StreamSource, FurnaceSlot, and FurnaceManager.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from furnace_manager import StreamSource, FurnaceSlot, FurnaceManager


class TestFurnaceManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_furnace_mgr_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_stream_source_mark_and_history(self):
        cfg = {
            "stream_id": "stream-1",
            "name": "Camera 1",
            "rtsp_url": "rtsp://dummy/1",
        }
        s = StreamSource(cfg)
        self.assertEqual(s.status, "unknown")
        self.assertEqual(len(s.history), 0)

        s.mark("active", "assigned to 4-1")
        self.assertEqual(s.status, "active")
        self.assertEqual(len(s.history), 1)
        self.assertEqual(s.history[0]["status"], "active")
        self.assertEqual(s.history[0]["note"], "assigned to 4-1")

        # Marking same status again should not duplicate history
        s.mark("active", "duplicate")
        self.assertEqual(len(s.history), 1)

        s.mark("faulty", "activefire timeout")
        self.assertEqual(s.status, "faulty")
        self.assertEqual(len(s.history), 2)

    @patch("furnace_manager.CameraWorker")
    def test_furnace_slot_attach_and_detach(self, mock_camera_worker_cls):
        mock_worker = MagicMock()
        mock_camera_worker_cls.return_value = mock_worker

        recorder = MagicMock()
        on_loss = MagicMock()
        cfg = {
            "furnace_id": "4/1",
            "name": "Furnace 4/1",
            "data_dir": os.path.join(self.tmp_dir, "heats_4_1"),
            "index_csv": os.path.join(self.tmp_dir, "heats_index_4_1.csv"),
            "heat_id_prefix": "Heat41",
            "influx_group": "SFC4/1",
            "pyro_field": "PYRO4/1",
        }
        slot = FurnaceSlot("4-1", cfg, recorder, on_loss)
        self.assertIsNone(slot.current_stream)
        self.assertIsNone(slot.state_machine)
        self.assertIsNone(slot.camera_worker)
        self.assertEqual(slot.pyro_field, "PYRO4/1")
        self.assertIsNotNone(slot.temperature_monitor)

        stream = StreamSource({
            "stream_id": "stream-1",
            "name": "Camera 1",
            "rtsp_url": "rtsp://dummy/1",
        })

        slot.attach_to_stream(stream)
        self.assertEqual(slot.current_stream, stream)
        self.assertEqual(stream.assigned_slot, "4-1")
        self.assertEqual(stream.status, "active")
        self.assertIsNotNone(slot.state_machine)
        self.assertIs(slot.state_machine.temperature_monitor, slot.temperature_monitor)
        self.assertIsNotNone(slot.camera_worker)
        mock_worker.start.assert_called_once()

        slot.detach()
        self.assertIsNone(slot.current_stream)
        self.assertIsNone(stream.assigned_slot)
        self.assertIsNone(slot.state_machine)
        self.assertIsNone(slot.camera_worker)
        mock_worker.stop.assert_called_once()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_furnace_manager_startup_with_maintenance(self, mock_probe, mock_worker_cls):
        # Simulate stream-1 showing maintenance (class 28)
        def fake_probe(rtsp_url, duration_seconds, *args, **kwargs):
            return "10.50.40.160" in rtsp_url or "stream-1" in rtsp_url

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        # stream-1 should be maintenance
        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        self.assertEqual(s1.status, "maintenance")
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, "5-1")
        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, "5-2")

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_furnace_manager_startup_and_runtime_reevaluation(self, mock_probe, mock_worker_cls):
        # Startup: no stream is maintenance
        # Re-evaluation: stream-2 and stream-3 show active fire; stream-1 does not
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_FURNACE_OPERATION_UNDERMAINTENANCE

        def fake_probe(rtsp_url, duration_seconds, target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE, *args, **kwargs):
            if target_class == CLS_FURNACE_OPERATION_UNDERMAINTENANCE:
                return False
            if target_class == CLS_FURNACE_ACTIVEFIRE:
                # stream-2 (158) and stream-3 (159) show fire, stream-1 (160) is cold
                return "10.50.40.158" in rtsp_url or "10.50.40.159" in rtsp_url
            return False

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        # Initial assignment: 5-1 -> s1, 5-2 -> s2, s3 is spare
        self.assertEqual(s1.status, "active")
        self.assertEqual(s1.assigned_slot, "5-1")
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, "5-2")
        self.assertEqual(s3.status, "spare")
        self.assertIsNone(s3.assigned_slot)

        slot_5_1 = mgr.get_slot("5-1")
        slot_5_2 = mgr.get_slot("5-2")
        initial_5_1_monitor = slot_5_1.temperature_monitor
        self.assertEqual(slot_5_1.pyro_field, "PYRO5/1")

        # Trigger active-fire loss on slot 5-1
        slot_5_1._handle_activefire_loss()

        # Wait for re-evaluation thread
        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        # After re-evaluation under priority rule: s1 released to spare, s2 now active on 5-1, s3 on 5-2
        self.assertEqual(s1.status, "spare")
        self.assertIsNone(s1.assigned_slot)
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, "5-1")
        self.assertEqual(slot_5_1.current_stream, s2)

        # Critical invariant: PYRO monitor and pyro_field on 5-1 survive the swap
        self.assertIs(slot_5_1.temperature_monitor, initial_5_1_monitor)
        self.assertEqual(slot_5_1.pyro_field, "PYRO5/1")
        self.assertIs(slot_5_1.state_machine.temperature_monitor, initial_5_1_monitor)

        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, "5-2")
        self.assertEqual(slot_5_2.current_stream, s3)

        # Snapshots
        snap = mgr.streams_snapshot()
        self.assertEqual(len(snap), 3)
        self.assertEqual(len(mgr.nav_slots()), 2)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream", return_value=False)
    def test_watchdog_tick_triggers_reevaluation_on_cold_idle_slot(self, mock_probe, mock_worker_cls):
        import datetime
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        slot_5_1 = mgr.get_slot("5-1")
        self.assertEqual(slot_5_1.state_machine.state, "IDLE")

        with patch.object(config, "ACTIVEFIRE_TIMEOUT_MINUTES", 5):
            # Set last_activefire_at to 6 minutes ago (> ACTIVEFIRE_TIMEOUT_MINUTES = 5)
            slot_5_1.state_machine.last_activefire_at = (
                datetime.datetime.now() - datetime.timedelta(minutes=6)
            )

            with patch.object(mgr, "_trigger_reevaluation") as mock_trigger:
                mgr._watchdog_tick()
                mock_trigger.assert_called_once_with(slot_5_1)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    def test_maintenance_reason_only_direct_classes(self):
        from camera_worker import (
            maintenance_reason,
            MAINTENANCE_DIRECT_CLASS_IDS,
            CLS_COIL_POSITION_PLACEDNEARFURNACE,
        )

        debouncer_mock = MagicMock()
        # Direct maintenance classes confirm maintenance
        for cid in sorted(MAINTENANCE_DIRECT_CLASS_IDS):
            reason = maintenance_reason([], debouncer_mock, [cid])
            self.assertIsNotNone(reason)
            self.assertEqual(reason, f"maintenance class {cid} confirmed")

        # Non-maintenance classes do not trigger maintenance
        reason_non_maint = maintenance_reason([], debouncer_mock, [9, 11, 3, 24])
        self.assertIsNone(reason_non_maint)

        # Empty newly_confirmed returns None
        self.assertIsNone(maintenance_reason([], debouncer_mock, []))

    def test_check_maintenance_streams_for_resume_clears_flag(self):
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        s1 = mgr.streams[0]
        s1.mark("maintenance", "test maintenance")
        from state_machine import CLS_FURNACE_ACTIVEFIRE

        with patch.object(mgr, "_probe_stream_for_process_resume", return_value=CLS_FURNACE_ACTIVEFIRE):
            with patch.object(mgr, "_trigger_reevaluation") as mock_reeval:
                mgr._check_maintenance_streams_for_resume()
                mock_reeval.assert_called_once()

        self.assertEqual(s1.status, "spare")
        self.assertIn(f"class {CLS_FURNACE_ACTIVEFIRE} confirmed", s1.history[-1]["note"])

    def test_check_maintenance_streams_for_resume_no_hit(self):
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        s1 = mgr.streams[0]
        s1.mark("maintenance", "test maintenance")

        with patch.object(mgr, "_probe_stream_for_process_resume", return_value=None):
            mgr._check_maintenance_streams_for_resume()

        self.assertEqual(s1.status, "maintenance")

    @patch.object(FurnaceManager, "_check_maintenance_streams_for_resume")
    def test_watchdog_tick_calls_check_maintenance_streams_for_resume(self, mock_check_resume):
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        mgr._watchdog_tick()
        mock_check_resume.assert_called_once()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_maintenance_detected_triggers_reevaluation_and_excludes_stream(self, mock_probe, mock_worker_cls):
        def fake_probe(rtsp_url, duration_seconds, target_class=None, maintenance_mode=False, *args, **kwargs):
            if maintenance_mode:
                return False
            return True

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        slot_5_1 = mgr.get_slot("5-1")

        slot_5_1._handle_maintenance_detected("maintenance class 28 confirmed")

        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        self.assertEqual(s1.status, "maintenance")
        self.assertIsNone(s1.assigned_slot)
        self.assertEqual(slot_5_1.current_stream.stream_id, "stream-2")

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("cv2.VideoCapture")
    @patch("detector.FurnaceDetector")
    def test_probe_stream_for_process_resume_activefire_priority(self, mock_det_cls, mock_cap_cls):
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_MAGNET_FEEDING_SCRAP
        import numpy as np

        mock_det = MagicMock()
        mock_det_cls.return_value = mock_det
        mock_det.detect.return_value = [
            {"class_id": CLS_FURNACE_ACTIVEFIRE},
            {"class_id": CLS_MAGNET_FEEDING_SCRAP},
        ]

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, np.zeros((10, 10, 3), dtype=np.uint8))
        mock_cap_cls.return_value = mock_cap

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        stream = mgr.streams[0]

        hit = mgr._probe_stream_for_process_resume(stream, duration_seconds=5)
        self.assertEqual(hit, CLS_FURNACE_ACTIVEFIRE)

    @patch("cv2.VideoCapture")
    @patch("detector.FurnaceDetector")
    def test_probe_stream_for_process_resume_other_resume_class(self, mock_det_cls, mock_cap_cls):
        from state_machine import CLS_MAGNET_FEEDING_SCRAP
        import numpy as np

        mock_det = MagicMock()
        mock_det_cls.return_value = mock_det
        mock_det.detect.return_value = [
            {"class_id": CLS_MAGNET_FEEDING_SCRAP},
        ]

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.read.return_value = (True, np.zeros((10, 10, 3), dtype=np.uint8))
        mock_cap_cls.return_value = mock_cap

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        stream = mgr.streams[0]

        hit = mgr._probe_stream_for_process_resume(stream, duration_seconds=5)
        self.assertEqual(hit, CLS_MAGNET_FEEDING_SCRAP)

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_priority_reevaluation_cases(self, mock_probe, mock_worker_cls):
        from state_machine import CLS_FURNACE_OPERATION_UNDERMAINTENANCE

        def fake_probe(rtsp_url, duration_seconds, target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE, *args, **kwargs):
            if target_class == CLS_FURNACE_OPERATION_UNDERMAINTENANCE:
                return False
            return True

        mock_probe.side_effect = fake_probe
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1, s2, s3 = mgr.streams[0], mgr.streams[1], mgr.streams[2]
        s1.mark("maintenance", "simulated maint")

        # Case: 1 under maint, 2 and 3 active -> 2->5/1, 3->5/2
        mgr._reevaluate_streams(mgr.slots["5-1"])
        self.assertEqual(mgr.slots["5-1"].current_stream, s2)
        self.assertEqual(mgr.slots["5-2"].current_stream, s3)
        self.assertEqual(s1.status, "maintenance")

        # Case: 1 resumes (now 1, 2, 3 are available) -> 1->5/1, 2->5/2, 3 spare
        s1.mark("spare", "resumed")
        mgr._reevaluate_streams(mgr.slots["5-1"])
        self.assertEqual(mgr.slots["5-1"].current_stream, s1)
        self.assertEqual(mgr.slots["5-2"].current_stream, s2)
        self.assertEqual(s3.status, "spare")

        # Case: 2 under maint (1 and 3 available) -> 1->5/1, 3->5/2, 2 maint
        s2.mark("maintenance", "simulated maint")
        mgr._reevaluate_streams(mgr.slots["5-1"])
        self.assertEqual(mgr.slots["5-1"].current_stream, s1)
        self.assertEqual(mgr.slots["5-2"].current_stream, s3)
        self.assertEqual(s2.status, "maintenance")

        # Case: 3 under maint (1 and 2 available) -> 1->5/1, 2->5/2, 3 maint
        s2.mark("spare", "resumed")
        s3.mark("maintenance", "simulated maint")
        mgr._reevaluate_streams(mgr.slots["5-1"])
        self.assertEqual(mgr.slots["5-1"].current_stream, s1)
        self.assertEqual(mgr.slots["5-2"].current_stream, s2)
        self.assertEqual(s3.status, "maintenance")

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch.object(FurnaceManager, "_probe_stream")
    def test_probe_for_maintenance_parallel_multiple_hits(self, mock_probe):
        def fake_probe(rtsp_url, *args, **kwargs):
            return "10.50.40.160" in rtsp_url or "10.50.40.158" in rtsp_url

        mock_probe.side_effect = fake_probe
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        hits = mgr._probe_for_maintenance()
        hit_ids = {s.stream_id for s in hits}
        self.assertEqual(hit_ids, {"stream-1", "stream-2"})

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_startup_with_multiple_maintenance_hits_assigns_both_slots(self, mock_probe, mock_worker_cls):
        # Simulate stream-1 having direct maintenance (class 28) and stream-2 having indirect maintenance (fan)
        def fake_probe(rtsp_url, duration_seconds, *args, **kwargs):
            if "10.50.40.160" in rtsp_url or "stream-1" in rtsp_url:
                return "maintenance class 28 confirmed"
            if "10.50.40.158" in rtsp_url or "stream-2" in rtsp_url:
                return "maintenance: negative fan detected inside furnace normal-position box"
            return False

        mock_probe.side_effect = fake_probe
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        # stream-1 has direct class 28 -> marked maintenance
        self.assertEqual(s1.status, "maintenance")
        # stream-2 and stream-3 back the two slots
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, "5-1")
        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, "5-2")

        # Both slots MUST have active camera workers
        self.assertIsNotNone(mgr.slots["5-1"].camera_worker)
        self.assertIsNotNone(mgr.slots["5-2"].camera_worker)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_reevaluate_streams_with_single_activefire_keeps_both_slots(self, mock_probe, mock_worker_cls):
        # Only stream-3 has active fire, stream-1 is in maintenance, stream-2 is idle (no fire)
        def fake_probe(rtsp_url, duration_seconds, target_class=None, maintenance_mode=False, *args, **kwargs):
            if target_class == 5:  # CLS_FURNACE_ACTIVEFIRE
                return "10.50.40.159" in rtsp_url or "stream-3" in rtsp_url
            return False

        mock_probe.side_effect = fake_probe
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1, s2, s3 = mgr.streams[0], mgr.streams[1], mgr.streams[2]
        s1.mark("maintenance", "direct maintenance")

        mgr._reevaluate_streams(mgr.slots["5-1"])

        # stream-3 (activefire) is assigned to 5-1; 5-2 is backfilled with stream-2
        self.assertEqual(mgr.slots["5-1"].current_stream, s3)
        self.assertEqual(mgr.slots["5-2"].current_stream, s2)
        self.assertIsNotNone(mgr.slots["5-1"].camera_worker)
        self.assertIsNotNone(mgr.slots["5-2"].camera_worker)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()


if __name__ == "__main__":
    unittest.main()


