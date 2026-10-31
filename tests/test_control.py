"""运行中控制：暂停 / 继续 / 停止 / 批准 / 重跑。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_team import control


class ControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.room = Path(self.tmp.name)
        self.original = (control.ROOM, control.CONTROL, control.PAUSE)
        control.ROOM = self.room
        control.CONTROL = self.room / "control.json"
        control.PAUSE = self.room / "pause.json"

    def tearDown(self) -> None:
        control.ROOM, control.CONTROL, control.PAUSE = self.original
        self.tmp.cleanup()


class RequestTests(ControlTestCase):
    def test_request_then_take(self) -> None:
        control.request("pause", "T-260101-000001")
        got = control.take()
        self.assertEqual(got["action"], "pause")
        self.assertEqual(got["task"], "T-260101-000001")
        self.assertIsNone(control.take(), "指令消费一次就该没了")

    def test_invalid_action_rejected(self) -> None:
        with self.assertRaises(ValueError):
            control.request("delete-everything")

    def test_command_for_other_task_is_kept(self) -> None:
        control.request("stop", "T-260101-999999")
        self.assertIsNone(control.take("T-260101-000001"))
        self.assertIsNotNone(control.pending(), "别的任务的指令不能被误消费")
        self.assertEqual(control.take("T-260101-999999")["action"], "stop")

    def test_command_without_task_applies_to_any(self) -> None:
        control.request("pause")
        self.assertEqual(control.take("T-260101-000001")["action"], "pause")


class GateTests(ControlTestCase):
    def test_no_command_goes_through(self) -> None:
        self.assertEqual(control.gate("T-260101-000001", "dispatch"), "go")

    def test_stop_raises(self) -> None:
        control.request("stop", "T-260101-000001")
        with self.assertRaises(control.TaskStopped):
            control.gate("T-260101-000001", "dispatch")

    def test_rerun_returns_rerun(self) -> None:
        control.request("rerun", "T-260101-000001")
        self.assertEqual(control.gate("T-260101-000001", "verify"), "rerun")

    def test_pause_blocks_until_resume(self) -> None:
        tid = "T-260101-000001"
        control.request("pause", tid)
        events: list[str] = []

        def resume_later() -> None:
            time.sleep(0.5)
            control.request("resume", tid)

        threading.Thread(target=resume_later, daemon=True).start()
        started = time.time()
        result = control.gate(tid, "dispatch", on_event=lambda ev, st: events.append(ev))
        self.assertEqual(result, "go")
        self.assertGreaterEqual(time.time() - started, 0.4, "暂停应该真的停住等")
        self.assertIn("paused", events)
        self.assertIn("resumed", events)
        self.assertIsNone(control.paused(), "继续之后暂停标记要清掉")

    def test_heartbeat_keeps_engine_alive_while_paused(self) -> None:
        tid = "T-260101-000001"
        control.request("pause", tid)
        beats: list[float] = []

        def resume_later() -> None:
            time.sleep(0.6)
            control.request("resume", tid)

        threading.Thread(target=resume_later, daemon=True).start()
        started = time.time()
        self.assertEqual(control.gate(tid, "verify", heartbeat=lambda: beats.append(time.time())), "go")
        self.assertGreaterEqual(time.time() - started, 0.5)
        self.assertGreaterEqual(len(beats), 2, "暂停期间应该持续报活，界面才不会显示引擎已死")

    def test_heartbeat_runs_during_approval_wait(self) -> None:
        tid = "T-260101-000001"
        beats: list[float] = []

        def approve_later() -> None:
            time.sleep(0.5)
            control.request("approve", tid)

        threading.Thread(target=approve_later, daemon=True).start()
        self.assertEqual(control.wait_approval(tid, heartbeat=lambda: beats.append(time.time())), "go")
        self.assertGreaterEqual(len(beats), 1, "等批准时也要报活")

    def test_pause_then_stop_raises(self) -> None:
        tid = "T-260101-000001"
        control.request("pause", tid)

        def stop_later() -> None:
            time.sleep(0.4)
            control.request("stop", tid)

        threading.Thread(target=stop_later, daemon=True).start()
        with self.assertRaises(control.TaskStopped):
            control.gate(tid, "dispatch")
        self.assertIsNone(control.paused())


class ApprovalTests(ControlTestCase):
    def test_approve_releases_gate(self) -> None:
        tid = "T-260101-000001"

        def approve_later() -> None:
            time.sleep(0.4)
            control.request("approve", tid)

        threading.Thread(target=approve_later, daemon=True).start()
        self.assertEqual(control.wait_approval(tid), "go")
        self.assertIsNone(control.paused())

    def test_stop_at_approval_raises(self) -> None:
        tid = "T-260101-000001"

        def stop_later() -> None:
            time.sleep(0.4)
            control.request("stop", tid)

        threading.Thread(target=stop_later, daemon=True).start()
        with self.assertRaises(control.TaskStopped):
            control.wait_approval(tid)

    def test_paused_marker_expires(self) -> None:
        control.set_paused("T-260101-000001", "test")
        self.assertIsNotNone(control.paused())
        data = control._read(control.PAUSE)  # noqa: SLF001
        data["ts"] = time.time() - 999999
        control.PAUSE.write_text(__import__("json").dumps(data), encoding="utf-8")
        self.assertIsNone(control.paused(), "过期暂停标记应自动失效")


if __name__ == "__main__":
    unittest.main()