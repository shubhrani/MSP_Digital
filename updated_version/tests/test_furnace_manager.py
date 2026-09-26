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
            return "10.50.40.153" in rtsp_url or "stream-1" in rtsp_url

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
        self.assertEqual(s2.assigned_slot, "4-1")
        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, "4-2")

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
                # stream-2 (152) and stream-3 (151) show fire, stream-1 (153) is cold
                return "10.50.40.152" in rtsp_url or "10.50.40.151" in rtsp_url
            return False

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        # Initial assignment: 4-1 -> s1, 4-2 -> s2, s3 is spare
        self.assertEqual(s1.status, "active")
        self.assertEqual(s1.assigned_slot, "4-1")
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, "4-2")
        self.assertEqual(s3.status, "spare")
        self.assertIsNone(s3.assigned_slot)

        slot_4_1 = mgr.get_slot("4-1")
        slot_4_2 = mgr.get_slot("4-2")
        initial_4_1_monitor = slot_4_1.temperature_monitor
        self.assertEqual(slot_4_1.pyro_field, "PYRO4/1")

        # Trigger active-fire loss on slot 4-1
        slot_4_1._handle_activefire_loss()

        # Wait for re-evaluation thread
        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        # After re-evaluation: s1 released to spare, s3 now active on 4-1, s2 unchanged on 4-2
        self.assertEqual(s1.status, "spare")
        self.assertIsNone(s1.assigned_slot)
        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, "4-1")
        self.assertEqual(slot_4_1.current_stream, s3)

        # Critical invariant: PYRO monitor and pyro_field on 4-1 survive the swap
        self.assertIs(slot_4_1.temperature_monitor, initial_4_1_monitor)
        self.assertEqual(slot_4_1.pyro_field, "PYRO4/1")
        self.assertIs(slot_4_1.state_machine.temperature_monitor, initial_4_1_monitor)

        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, "4-2")
        self.assertEqual(slot_4_2.current_stream, s2)

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

        slot_4_1 = mgr.get_slot("4-1")
        self.assertEqual(slot_4_1.state_machine.state, "IDLE")

        # Set last_activefire_at to 6 minutes ago (> ACTIVEFIRE_TIMEOUT_MINUTES = 5)
        slot_4_1.state_machine.last_activefire_at = (
            datetime.datetime.now() - datetime.timedelta(minutes=6)
        )

        with patch.object(mgr, "_trigger_reevaluation") as mock_trigger:
            mgr._watchdog_tick()
            mock_trigger.assert_called_once_with(slot_4_1)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_reevaluation_runtime_class_28_maintenance(self, mock_probe, mock_worker_cls):
        """Scenario A: Slot 4-1's camera confirms class 28 at runtime.
        Re-evaluation disqualifies stream-1 (maintenance), confirms stream-2 and
        stream-3 (hot). 4-2 stays on stream-2; 4-1 swaps onto stream-3."""
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_FURNACE_OPERATION_UNDERMAINTENANCE

        def fake_probe(rtsp_url, duration_seconds, target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE, *args, **kwargs):
            if target_class == CLS_FURNACE_OPERATION_UNDERMAINTENANCE:
                # stream-1 shows maintenance
                return "10.50.40.153" in rtsp_url or "stream-1" in rtsp_url
            if target_class == CLS_FURNACE_ACTIVEFIRE:
                # stream-2 and stream-3 show active fire
                return "10.50.40.152" in rtsp_url or "10.50.40.151" in rtsp_url
            return False

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        # Startup with no maintenance for this test
        with patch.object(mgr, "_probe_for_maintenance", return_value=[]):
            with patch.object(FurnaceSlot, "start_monitor"):
                mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        self.assertEqual(s1.assigned_slot, "4-1")
        self.assertEqual(s2.assigned_slot, "4-2")
        self.assertIsNone(s3.assigned_slot)

        slot_4_1 = mgr.get_slot("4-1")
        slot_4_2 = mgr.get_slot("4-2")

        # Trigger runtime maintenance callback on slot 4-1
        slot_4_1._handle_maintenance_detected()

        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        # Result: stream-1 is maintenance, 4-2 stays on stream-2, 4-1 moves to stream-3
        self.assertEqual(s1.status, "maintenance")
        self.assertIsNone(s1.assigned_slot)

        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, "4-2")
        self.assertEqual(slot_4_2.current_stream, s2)

        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, "4-1")
        self.assertEqual(slot_4_1.current_stream, s3)
        self.assertEqual(slot_4_1.pyro_field, "PYRO4/1")

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_reevaluation_aborts_when_fewer_than_two_eligible(self, mock_probe, mock_worker_cls):
        """Scenario C: Only 1 stream is hot (stream-3), stream-1 is maintenance,
        stream-2 is cold. Fewer than 2 eligible -> abort re-evaluation, leave assignments untouched."""
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_FURNACE_OPERATION_UNDERMAINTENANCE

        def fake_probe(rtsp_url, duration_seconds, target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE, *args, **kwargs):
            if target_class == CLS_FURNACE_OPERATION_UNDERMAINTENANCE:
                return "10.50.40.153" in rtsp_url or "stream-1" in rtsp_url
            if target_class == CLS_FURNACE_ACTIVEFIRE:
                # Only stream-3 has active fire; stream-2 is cold
                return "10.50.40.151" in rtsp_url
            return False

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(mgr, "_probe_for_maintenance", return_value=[]):
            with patch.object(FurnaceSlot, "start_monitor"):
                mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        slot_4_1 = mgr.get_slot("4-1")
        slot_4_2 = mgr.get_slot("4-2")

        slot_4_1._handle_maintenance_detected()

        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        # Abort: assignments left as-is, swap flag cleared
        self.assertEqual(s1.status, "maintenance")  # Tagged maintenance by probe
        self.assertEqual(slot_4_1.current_stream, s1)
        self.assertEqual(slot_4_2.current_stream, s2)
        self.assertFalse(slot_4_1._swap_in_progress)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()


if __name__ == "__main__":
    unittest.main()
