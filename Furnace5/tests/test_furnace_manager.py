"""
Unit tests for furnace_manager.py: StreamSource, FurnaceSlot, and FurnaceManager.
"""

import datetime
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
from state_machine import (
    CLS_MAGNET_FEEDING_SCRAP,
    CLS_SPONGE_FEEDER_ON_FURNACE,
    CLS_FURNACE_TILTING,
    CLS_FURNACE_NORMAL,
)


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
        s1_url = config.STREAMS[0]["rtsp_url"]
        def fake_probe(rtsp_url, duration_seconds, *args, **kwargs):
            return s1_url in rtsp_url or "stream-1" in rtsp_url

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        # stream-1 should be maintenance
        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        slot1, slot2 = list(config.FURNACE_SLOTS.keys())[:2]
        self.assertEqual(s1.status, "maintenance")
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, slot1)
        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, slot2)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_furnace_manager_startup_and_runtime_reevaluation(self, mock_probe, mock_worker_cls):
        # Startup: no stream is maintenance
        # Re-evaluation: stream-2 and stream-3 show active fire; stream-1 does not
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_FURNACE_OPERATION_UNDERMAINTENANCE

        s2_url = config.STREAMS[1]["rtsp_url"]
        s3_url = config.STREAMS[2]["rtsp_url"]
        slot1, slot2 = list(config.FURNACE_SLOTS.keys())[:2]
        expected_pyro1 = config.FURNACE_SLOTS[slot1]["pyro_field"]

        def fake_probe(rtsp_url, duration_seconds, target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE, *args, **kwargs):
            if target_class == CLS_FURNACE_OPERATION_UNDERMAINTENANCE:
                return False
            if target_class == CLS_FURNACE_ACTIVEFIRE:
                # stream-2 and stream-3 show fire, stream-1 is cold
                return s2_url in rtsp_url or s3_url in rtsp_url
            return False

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)

        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        # Initial assignment: slot1 -> s1, slot2 -> s2, s3 is spare
        self.assertEqual(s1.status, "active")
        self.assertEqual(s1.assigned_slot, slot1)
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, slot2)
        self.assertEqual(s3.status, "spare")
        self.assertIsNone(s3.assigned_slot)

        slot_obj_1 = mgr.get_slot(slot1)
        slot_obj_2 = mgr.get_slot(slot2)
        initial_1_monitor = slot_obj_1.temperature_monitor
        self.assertEqual(slot_obj_1.pyro_field, expected_pyro1)

        # Trigger active-fire loss on slot 1
        slot_obj_1._handle_activefire_loss()

        # Wait for re-evaluation thread
        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        # After re-evaluation under priority rule: s1 released to spare, s2 now active on slot1, s3 on slot2
        self.assertEqual(s1.status, "spare")
        self.assertIsNone(s1.assigned_slot)
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, slot1)
        self.assertEqual(slot_obj_1.current_stream, s2)

        # Critical invariant: PYRO monitor and pyro_field on slot1 survive the swap
        self.assertIs(slot_obj_1.temperature_monitor, initial_1_monitor)
        self.assertEqual(slot_obj_1.pyro_field, expected_pyro1)
        self.assertIs(slot_obj_1.state_machine.temperature_monitor, initial_1_monitor)

        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, slot2)
        self.assertEqual(slot_obj_2.current_stream, s3)

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

        slot1 = list(config.FURNACE_SLOTS.keys())[0]
        slot_obj_1 = mgr.get_slot(slot1)
        self.assertEqual(slot_obj_1.state_machine.state, "IDLE")

        with patch.object(config, "ACTIVEFIRE_TIMEOUT_MINUTES", 5):
            # Set last_activefire_at to 6 minutes ago (> ACTIVEFIRE_TIMEOUT_MINUTES = 5)
            slot_obj_1.state_machine.last_activefire_at = (
                datetime.datetime.now() - datetime.timedelta(minutes=6)
            )

            with patch.object(mgr, "_trigger_reevaluation") as mock_trigger:
                mgr._watchdog_tick()
                mock_trigger.assert_called_once_with(slot_obj_1)

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

    def test_all_specified_maintenance_classes_trigger_maintenance(self):
        from camera_worker import (
            maintenance_reason,
            MAINTENANCE_DIRECT_CLASS_IDS,
        )
        from state_machine import (
            CLS_COIL_POSITION_PLACEDNEARFURNACE,
            CLS_FURNACE_OPERATION_PREHEATING,
            CLS_FURNACE_OPERATION_UNDERMAINTENANCE,
            CLS_FURMA_POSITION_PLACEDNEARFURNACE,
        )

        expected_classes = {
            CLS_COIL_POSITION_PLACEDNEARFURNACE: 0,
            CLS_FURNACE_OPERATION_PREHEATING: 7,
            CLS_FURNACE_OPERATION_UNDERMAINTENANCE: 28,
            CLS_FURMA_POSITION_PLACEDNEARFURNACE: 39,
        }

        debouncer_mock = MagicMock()
        for cid, expected_val in expected_classes.items():
            self.assertEqual(cid, expected_val)
            self.assertIn(cid, MAINTENANCE_DIRECT_CLASS_IDS)
            reason = maintenance_reason([], debouncer_mock, [cid])
            self.assertEqual(reason, f"maintenance class {cid} confirmed")

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
        slot1 = list(config.FURNACE_SLOTS.keys())[0]
        slot_obj_1 = mgr.get_slot(slot1)

        slot_obj_1._handle_maintenance_detected("maintenance class 28 confirmed")

        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        self.assertEqual(s1.status, "maintenance")
        self.assertIsNone(s1.assigned_slot)
        self.assertEqual(slot_obj_1.current_stream.stream_id, "stream-2")

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

        slot1, slot2 = list(config.FURNACE_SLOTS.keys())[:2]
        s1, s2, s3 = mgr.streams[0], mgr.streams[1], mgr.streams[2]
        s1.mark("maintenance", "simulated maint")

        # Case: 1 under maint, 2 and 3 active -> 2->slot1, 3->slot2
        mgr._reevaluate_streams(mgr.slots[slot1])
        self.assertEqual(mgr.slots[slot1].current_stream, s2)
        self.assertEqual(mgr.slots[slot2].current_stream, s3)
        self.assertEqual(s1.status, "maintenance")

        # Case: 1 resumes (now 1, 2, 3 are available) -> 1->slot1, 2->slot2, 3 spare
        s1.mark("spare", "resumed")
        mgr._reevaluate_streams(mgr.slots[slot1])
        self.assertEqual(mgr.slots[slot1].current_stream, s1)
        self.assertEqual(mgr.slots[slot2].current_stream, s2)
        self.assertEqual(s3.status, "spare")

        # Case: 2 under maint (1 and 3 available) -> 1->slot1, 3->slot2, 2 maint
        s2.mark("maintenance", "simulated maint")
        mgr._reevaluate_streams(mgr.slots[slot1])
        self.assertEqual(mgr.slots[slot1].current_stream, s1)
        self.assertEqual(mgr.slots[slot2].current_stream, s3)
        self.assertEqual(s2.status, "maintenance")

        # Case: 3 under maint (1 and 2 available) -> 1->slot1, 2->slot2, 3 maint
        s2.mark("spare", "resumed")
        s3.mark("maintenance", "simulated maint")
        mgr._reevaluate_streams(mgr.slots[slot1])
        self.assertEqual(mgr.slots[slot1].current_stream, s1)
        self.assertEqual(mgr.slots[slot2].current_stream, s2)
        self.assertEqual(s3.status, "maintenance")

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch.object(FurnaceManager, "_probe_stream")
    def test_probe_for_maintenance_parallel_multiple_hits(self, mock_probe):
        s1_url = config.STREAMS[0]["rtsp_url"]
        s2_url = config.STREAMS[1]["rtsp_url"]

        def fake_probe(rtsp_url, *args, **kwargs):
            return s1_url in rtsp_url or s2_url in rtsp_url

        mock_probe.side_effect = fake_probe
        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        hits = mgr._probe_for_maintenance()
        hit_ids = {s.stream_id for s in hits}
        self.assertEqual(hit_ids, {"stream-1", "stream-2"})

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_swap_with_cold_spare_stream_maintains_both_slots_and_detection(self, mock_probe, mock_worker_cls):
        """Regression test for user issue:
        When an assigned stream (e.g. stream-1) enters maintenance, the spare stream (stream-3)
        does not have active fire yet (it's cold/idle waiting for scrap charging).
        Re-evaluation must staff slot 4-1 with stream-2 and slot 4-2 with stream-3,
        rather than dropping stream-3 and leaving slot 4-2 unassigned with a blank feed.
        """
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_FURNACE_OPERATION_UNDERMAINTENANCE

        s2_url = config.STREAMS[1]["rtsp_url"]
        slot1, slot2 = list(config.FURNACE_SLOTS.keys())[:2]

        def fake_probe(rtsp_url, duration_seconds, target_class=CLS_FURNACE_OPERATION_UNDERMAINTENANCE, *args, **kwargs):
            if target_class == CLS_FURNACE_OPERATION_UNDERMAINTENANCE:
                return False
            if target_class == CLS_FURNACE_ACTIVEFIRE:
                # stream-2 has active fire; spare stream-3 is cold/idle (NO active fire)
                return s2_url in rtsp_url or "stream-2" in rtsp_url
            return False

        mock_probe.side_effect = fake_probe

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
        s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
        s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

        slot_obj_1 = mgr.get_slot(slot1)
        slot_obj_2 = mgr.get_slot(slot2)

        # Initial state before swap
        self.assertEqual(slot_obj_1.current_stream, s1)
        self.assertEqual(slot_obj_2.current_stream, s2)
        self.assertEqual(s3.status, "spare")

        # Trigger maintenance swap on slot 1
        slot_obj_1._handle_maintenance_detected("maintenance class 28 confirmed")
        if mgr._reeval_thread:
            mgr._reeval_thread.join(timeout=5.0)

        # Slot 1 now gets stream-2, Slot 2 gets spare stream-3 even though stream-3 had NO active fire!
        self.assertEqual(s1.status, "maintenance")
        self.assertIsNone(s1.assigned_slot)
        self.assertEqual(s2.status, "active")
        self.assertEqual(s2.assigned_slot, slot1)
        self.assertEqual(slot_obj_1.current_stream, s2)
        self.assertIsNotNone(slot_obj_1.camera_worker)
        self.assertIsNotNone(slot_obj_1.state_machine)

        # Slot 2 MUST NOT be None or unassigned
        self.assertEqual(s3.status, "active")
        self.assertEqual(s3.assigned_slot, slot2)
        self.assertEqual(slot_obj_2.current_stream, s3)
        self.assertIsNotNone(slot_obj_2.camera_worker)
        self.assertIsNotNone(slot_obj_2.state_machine)

        # Swap flags must be cleared
        self.assertFalse(slot_obj_1._swap_in_progress)
        self.assertFalse(slot_obj_2._swap_in_progress)

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_check_cold_streams_for_hot_spares(self, mock_probe, mock_worker_cls):
        """Test that if an assigned slot has been cold, and an unassigned spare warms up,
        re-evaluation is triggered for the cold slot."""
        import datetime
        from state_machine import CLS_FURNACE_ACTIVEFIRE

        # Return False during startup probe (no stream is in maintenance)
        mock_probe.return_value = False

        recorder = MagicMock()
        mgr = FurnaceManager(recorder)
        with patch.object(FurnaceSlot, "start_monitor"):
            mgr.start()

        slot1, slot2 = list(config.FURNACE_SLOTS.keys())[:2]
        cold_slot = mgr.get_slot(slot2)
        self.assertIsNotNone(cold_slot.state_machine)
        cold_slot.state_machine.last_activefire_at = (
            datetime.datetime.now() - datetime.timedelta(minutes=config.COLD_STREAM_MIN_COLD_MINUTES + 1)
        )

        spare_stream = next(s for s in mgr.streams if s.assigned_slot is None)
        self.assertIsNotNone(spare_stream)

        # Mock probe to return True for active fire
        mock_probe.return_value = True

        with patch.object(mgr, "_trigger_reevaluation") as mock_trigger:
            mgr._check_cold_streams_for_hot_spares()
            mock_probe.assert_called_with(
                spare_stream.rtsp_url,
                config.PROBE_ACTIVEFIRE_SECONDS,
                target_class=CLS_FURNACE_ACTIVEFIRE,
            )
            mock_trigger.assert_called_once_with(cold_slot)

        # Immediate next call should be throttled by COLD_STREAM_WATCH_SECONDS
        mock_probe.reset_mock()
        with patch.object(mgr, "_trigger_reevaluation") as mock_trigger:
            mgr._check_cold_streams_for_hot_spares()
            mock_probe.assert_not_called()
            mock_trigger.assert_not_called()

        with patch.object(FurnaceSlot, "stop_monitor"):
            mgr.stop()

    @patch("furnace_manager.CameraWorker")
    def test_stream_swap_preserves_heat_and_tilts(self, mock_cw):
        recorder = MagicMock()
        cfg = {
            "furnace_id": "5/1",
            "name": "Furnace 5/1",
            "data_dir": os.path.join(self.tmp_dir, "heats_5_1"),
            "index_csv": os.path.join(self.tmp_dir, "heats_index_5_1.csv"),
            "heat_id_prefix": "Heat51",
            "influx_group": "SFC5/1",
            "pyro_field": "PYRO5/1",
        }
        slot = FurnaceSlot("5-1", cfg, recorder, on_activefire_loss=MagicMock())
        stream1 = StreamSource({"stream_id": "stream-1", "name": "Camera 1", "rtsp_url": "rtsp://dummy/1"})
        stream2 = StreamSource({"stream_id": "stream-2", "name": "Camera 2", "rtsp_url": "rtsp://dummy/2"})

        slot.attach_to_stream(stream1)
        sm = slot.state_machine

        # Start a heat and record a tilt on stream 1
        t0 = datetime.datetime.now()
        sm.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t0)
        heat_id = sm.heat_id
        sm.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t0 + datetime.timedelta(minutes=5))
        sm.process_detections({CLS_FURNACE_TILTING}, t0 + datetime.timedelta(minutes=10))
        sm.process_detections({CLS_FURNACE_NORMAL}, t0 + datetime.timedelta(minutes=10, seconds=30))
        self.assertEqual(sm.tilt_count, 1)
        self.assertEqual(sm.state, sm.STATE_SPONGE_CHARGING)

        # Mid-process stream swap to stream 2
        slot.attach_to_stream(stream2)

        # State machine and in-flight heat must be preserved!
        self.assertIs(slot.state_machine, sm)
        self.assertEqual(slot.state_machine.heat_id, heat_id)
        self.assertEqual(slot.state_machine.state, sm.STATE_SPONGE_CHARGING)
        self.assertEqual(slot.state_machine.tilt_count, 1)

        # Record another tilt on stream 2
        sm.process_detections({CLS_FURNACE_TILTING}, t0 + datetime.timedelta(minutes=15))
        sm.process_detections({CLS_FURNACE_NORMAL}, t0 + datetime.timedelta(minutes=15, seconds=30))
        self.assertEqual(slot.state_machine.tilt_count, 2)

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_process_shift_on_maintenance_recovery_incomplete_3rd_rtsp(self, mock_probe, mock_cw):
        """When rtsp2 is on slot1 and rtsp3 on slot2, and rtsp1 comes out of maintenance:
        - Process of slot1 shifts to slot2 with rtsp2
        - Current incomplete process of 3rd rtsp shifts to slot1 with rtsp1
        """
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_MAGNET_FEEDING_SCRAP, CLS_SPONGE_FEEDER_ON_FURNACE

        # Stream 1 in maintenance initially
        def fake_probe(rtsp_url, duration_seconds, *args, **kwargs):
            return "stream-1" in rtsp_url or "10.50.40.158" in rtsp_url

        mock_probe.side_effect = fake_probe

        test_slots_cfg = {
            "5-1": {
                "furnace_id": "5/1",
                "name": "Furnace 5/1",
                "data_dir": os.path.join(self.tmp_dir, "heats_5_1"),
                "index_csv": os.path.join(self.tmp_dir, "heats_index_5_1.csv"),
                "heat_id_prefix": "Heat51",
                "influx_group": "SFC5/1",
                "pyro_field": "PYRO5/1",
            },
            "5-2": {
                "furnace_id": "5/2",
                "name": "Furnace 5/2",
                "data_dir": os.path.join(self.tmp_dir, "heats"),
                "index_csv": os.path.join(self.tmp_dir, "heats_index.csv"),
                "heat_id_prefix": "Heat",
                "influx_group": "SFC5/2",
                "pyro_field": "PYRO5/2",
            },
        }

        with patch.dict(config.FURNACE_SLOTS, test_slots_cfg, clear=True):
            recorder = MagicMock()
            mgr = FurnaceManager(recorder)

            with patch.object(FurnaceSlot, "start_monitor"):
                mgr.start()

            s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
            s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
            s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

            slot_5_1 = mgr.get_slot("5-1")
            slot_5_2 = mgr.get_slot("5-2")

            # Initial state: s1 maintenance, s2 on 5-1, s3 on 5-2
            self.assertEqual(s1.status, "maintenance")
            self.assertEqual(slot_5_1.current_stream, s2)
            self.assertEqual(slot_5_2.current_stream, s3)

            # Start Heat 1 on slot 5-1 (with stream-2)
            t0 = datetime.datetime.now()
            sm_slot1 = slot_5_1.state_machine
            sm_slot1.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t0)
            sm_slot1.process_detections({CLS_SPONGE_FEEDER_ON_FURNACE}, t0 + datetime.timedelta(minutes=5))
            heat1_id = sm_slot1.heat_id
            self.assertEqual(sm_slot1.state, sm_slot1.STATE_SPONGE_CHARGING)

            # Start Heat 2 on slot 5-2 (with stream-3)
            sm_slot2 = slot_5_2.state_machine
            sm_slot2.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t0)
            heat2_id = sm_slot2.heat_id
            self.assertEqual(sm_slot2.state, sm_slot2.STATE_SCRAP_CHARGING)

            # Now simulate rtsp1 getting out of maintenance:
            def fake_probe_after(rtsp_url, duration_seconds, target_class=None, *args, **kwargs):
                if target_class == CLS_FURNACE_ACTIVEFIRE:
                    return "10.50.40.158" in rtsp_url or "10.50.40.159" in rtsp_url or "stream-1" in rtsp_url or "stream-2" in rtsp_url
                return False

            mock_probe.side_effect = fake_probe_after

            # Clear maintenance flag on stream-1
            s1.mark("spare", "maintenance cleared")

            # Trigger re-evaluation
            mgr._reevaluate_streams(slot_5_1)

            # Invariant checks:
            # 1. rtsp2 is now on slot 5-2
            self.assertEqual(slot_5_2.current_stream, s2)
            # Process of slot 1 shifted to slot 5-2 with rtsp2!
            self.assertIs(slot_5_2.state_machine, sm_slot1)
            self.assertEqual(slot_5_2.state_machine.heat_id, heat1_id)
            self.assertEqual(slot_5_2.state_machine.state, sm_slot1.STATE_SPONGE_CHARGING)
            self.assertEqual(slot_5_2.state_machine.furnace_id, "5/2")
            self.assertIs(slot_5_2.state_machine.temperature_monitor, slot_5_2.temperature_monitor)

            # 2. rtsp1 is now on slot 5-1
            self.assertEqual(slot_5_1.current_stream, s1)
            # Current incomplete process of 3rd rtsp (from slot 5-2) shifted to slot 5-1!
            self.assertIs(slot_5_1.state_machine, sm_slot2)
            self.assertEqual(slot_5_1.state_machine.heat_id, heat2_id)
            self.assertEqual(slot_5_1.state_machine.state, sm_slot2.STATE_SCRAP_CHARGING)
            self.assertEqual(slot_5_1.state_machine.furnace_id, "5/1")
            self.assertIs(slot_5_1.state_machine.temperature_monitor, slot_5_1.temperature_monitor)

            with patch.object(FurnaceSlot, "stop_monitor"):
                mgr.stop()

    @patch("furnace_manager.CameraWorker")
    @patch.object(FurnaceManager, "_probe_stream")
    def test_process_shift_on_maintenance_recovery_completed_3rd_rtsp(self, mock_probe, mock_cw):
        """When 3rd rtsp process is completed (IDLE), slot1 receives a fresh state machine."""
        from state_machine import CLS_FURNACE_ACTIVEFIRE, CLS_MAGNET_FEEDING_SCRAP

        def fake_probe(rtsp_url, duration_seconds, *args, **kwargs):
            return "stream-1" in rtsp_url or "10.50.40.158" in rtsp_url

        mock_probe.side_effect = fake_probe

        test_slots_cfg = {
            "5-1": {
                "furnace_id": "5/1",
                "name": "Furnace 5/1",
                "data_dir": os.path.join(self.tmp_dir, "heats_5_1"),
                "index_csv": os.path.join(self.tmp_dir, "heats_index_5_1.csv"),
                "heat_id_prefix": "Heat51",
                "influx_group": "SFC5/1",
                "pyro_field": "PYRO5/1",
            },
            "5-2": {
                "furnace_id": "5/2",
                "name": "Furnace 5/2",
                "data_dir": os.path.join(self.tmp_dir, "heats"),
                "index_csv": os.path.join(self.tmp_dir, "heats_index.csv"),
                "heat_id_prefix": "Heat",
                "influx_group": "SFC5/2",
                "pyro_field": "PYRO5/2",
            },
        }

        with patch.dict(config.FURNACE_SLOTS, test_slots_cfg, clear=True):
            recorder = MagicMock()
            mgr = FurnaceManager(recorder)

            with patch.object(FurnaceSlot, "start_monitor"):
                mgr.start()

            s1 = next(s for s in mgr.streams if s.stream_id == "stream-1")
            s2 = next(s for s in mgr.streams if s.stream_id == "stream-2")
            s3 = next(s for s in mgr.streams if s.stream_id == "stream-3")

            slot_5_1 = mgr.get_slot("5-1")
            slot_5_2 = mgr.get_slot("5-2")

            # Slot 5-1 has an active heat
            t0 = datetime.datetime.now()
            sm_slot1 = slot_5_1.state_machine
            sm_slot1.process_detections({CLS_MAGNET_FEEDING_SCRAP}, t0)
            heat1_id = sm_slot1.heat_id

            # Slot 5-2 is IDLE (process completed)
            self.assertEqual(slot_5_2.state_machine.state, slot_5_2.state_machine.STATE_IDLE)

            def fake_probe_after(rtsp_url, duration_seconds, target_class=None, *args, **kwargs):
                if target_class == CLS_FURNACE_ACTIVEFIRE:
                    return "10.50.40.158" in rtsp_url or "10.50.40.159" in rtsp_url or "stream-1" in rtsp_url or "stream-2" in rtsp_url
                return False

            mock_probe.side_effect = fake_probe_after
            s1.mark("spare", "maintenance cleared")

            # Trigger re-evaluation
            mgr._reevaluate_streams(slot_5_1)

            # Slot 5-2 gets slot 1's active heat
            self.assertEqual(slot_5_2.current_stream, s2)
            self.assertIs(slot_5_2.state_machine, sm_slot1)
            self.assertEqual(slot_5_2.state_machine.heat_id, heat1_id)

            # Slot 5-1 gets stream-1 and is IDLE (fresh SM, since 3rd rtsp had no incomplete heat)
            self.assertEqual(slot_5_1.current_stream, s1)
            self.assertEqual(slot_5_1.state_machine.state, slot_5_1.state_machine.STATE_IDLE)
            self.assertIsNone(slot_5_1.state_machine.heat_id)

            with patch.object(FurnaceSlot, "stop_monitor"):
                mgr.stop()


if __name__ == "__main__":
    unittest.main()


