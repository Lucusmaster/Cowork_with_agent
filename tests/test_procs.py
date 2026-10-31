"""进程存活 / 心跳状态 / 锁接管：这三件事必须分清楚。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_team import procs, start_all, worker


class PidAliveTests(unittest.TestCase):
    def test_self_is_alive(self) -> None:
        self.assertTrue(procs.pid_alive(os.getpid()))

    def test_missing_pid_is_dead(self) -> None:
        self.assertFalse(procs.pid_alive(999999))

    def test_silly_values(self) -> None:
        self.assertFalse(procs.pid_alive(0))
        self.assertFalse(procs.pid_alive(-5))
        self.assertFalse(procs.pid_alive("不是数字"))


class LockStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "worker.lock"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, pid: int, age: float) -> None:
        self.lock.write_text(json.dumps({"pid": pid, "beat": time.time() - age}), encoding="utf-8")

    def test_missing_file(self) -> None:
        self.assertEqual(procs.lock_state(self.lock)["status"], "none")

    def test_dead_process(self) -> None:
        self._write(999999, 5)
        state = procs.lock_state(self.lock)
        self.assertFalse(state["alive"])
        self.assertEqual(state["status"], "dead")

    def test_fresh_heartbeat(self) -> None:
        self._write(os.getpid(), 1)
        state = procs.lock_state(self.lock)
        self.assertTrue(state["alive"])
        self.assertFalse(state["stale"])
        self.assertIn(state["status"], {"running", "busy"})

    def test_stale_heartbeat_but_process_alive(self) -> None:
        """长任务超过 90 秒没刷新心跳：进程还在，不能算死。"""
        self._write(os.getpid(), 200)
        state = procs.lock_state(self.lock)
        self.assertTrue(state["alive"], "进程还活着")
        self.assertTrue(state["stale"], "心跳确实旧了")
        self.assertEqual(state["status"], "stalled")

    def test_broken_file(self) -> None:
        self.lock.write_text("不是 JSON", encoding="utf-8")
        self.assertEqual(procs.lock_state(self.lock)["status"], "none")


class TakeLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._saved = (worker.LOCK, worker.ROOM)
        worker.LOCK = base / "worker.lock"
        worker.ROOM = base
        worker.log = lambda *a, **k: None  # noqa: ARG005

    def tearDown(self) -> None:
        worker.LOCK, worker.ROOM = self._saved
        self.tmp.cleanup()

    def test_refuses_when_process_alive_even_if_heartbeat_stale(self) -> None:
        worker.LOCK.write_text(json.dumps({"pid": os.getpid(), "beat": time.time() - 600}),
                               encoding="utf-8")
        self.assertFalse(worker.take_lock(), "心跳旧但进程活着，不能抢锁（会双开）")

    def test_takes_over_dead_lock(self) -> None:
        worker.LOCK.write_text(json.dumps({"pid": 999999, "beat": time.time() - 600}),
                               encoding="utf-8")
        self.assertTrue(worker.take_lock())
        info = json.loads(worker.LOCK.read_text(encoding="utf-8"))
        self.assertEqual(info["pid"], os.getpid())

    def test_first_start(self) -> None:
        self.assertTrue(worker.take_lock())


class HeartbeatThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (worker.LOCK, worker.ROOM)
        worker.LOCK = Path(self.tmp.name) / "worker.lock"
        worker.ROOM = Path(self.tmp.name)

    def tearDown(self) -> None:
        worker.stop_heartbeat()
        worker.LOCK, worker.ROOM = self._saved
        self.tmp.cleanup()

    def test_heartbeat_keeps_beating_while_main_loop_is_blocked(self) -> None:
        """模拟主循环被长任务占住：心跳线程必须继续刷新。"""
        worker.start_heartbeat(interval=0.05)
        try:
            time.sleep(0.12)
            first = procs.read_lock(worker.LOCK)["beat"]
            time.sleep(0.4)          # 主循环在这段时间里什么都不做
            second = procs.read_lock(worker.LOCK)["beat"]
        finally:
            worker.stop_heartbeat()
        self.assertGreater(second, first, "心跳应该在后台上继续刷新")
        self.assertLess(time.time() - second, 30, "心跳不该过期")

    def test_stop_heartbeat_ends_thread(self) -> None:
        thread = worker.start_heartbeat(interval=0.05)
        self.assertTrue(thread.is_alive())
        worker.stop_heartbeat()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


class LauncherTests(unittest.TestCase):
    """start_all 的 status/stop 必须认进程，不能只认心跳。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._saved = (start_all.LOCK, start_all.ROOT)
        start_all.LOCK = base / "worker.lock"
        start_all.ROOT = base

    def tearDown(self) -> None:
        start_all.LOCK, start_all.ROOT = self._saved
        self.tmp.cleanup()

    def _lock(self, pid: int, age: float) -> None:
        start_all.LOCK.write_text(
            json.dumps({"pid": pid, "beat": time.time() - age, "root": str(start_all.ROOT)}),
            encoding="utf-8")

    def test_pid_reported_even_if_heartbeat_stale(self) -> None:
        self._lock(os.getpid(), 200)
        self.assertEqual(start_all.worker_pid(), os.getpid(),
                         "心跳旧但进程活着，必须认它，否则 stop 杀不掉")

    def test_dead_pid_not_reported(self) -> None:
        self._lock(999999, 5)
        self.assertEqual(start_all.worker_pid(), 0)

    def test_stop_kills_stalled_worker(self) -> None:
        self._lock(os.getpid(), 300)
        killed: list[int] = []
        original = start_all.kill_pid
        start_all.kill_pid = lambda pid: killed.append(pid)
        try:
            start_all.stop(port=1)
        finally:
            start_all.kill_pid = original
        self.assertIn(os.getpid(), killed, "心跳停滞的进程也必须被杀掉")

    def test_lock_from_other_repo_is_ignored(self) -> None:
        start_all.LOCK.write_text(
            json.dumps({"pid": os.getpid(), "beat": time.time(), "root": "C:/somewhere-else"}),
            encoding="utf-8")
        self.assertEqual(start_all.worker_pid(), 0)


if __name__ == "__main__":
    unittest.main()

class ReadLockRetryTests(unittest.TestCase):
    """Windows 上 os.replace 的瞬间读文件会 PermissionError，必须重试而不是当成没引擎。"""

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(procs.read_lock(Path(tmp) / "nope.lock"), {})

    def test_transient_permission_error_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "worker.lock"
            lock.write_text(json.dumps({"pid": 1, "beat": 2.0}), encoding="utf-8")
            calls = {"n": 0}
            real = Path.read_text

            def flaky(self, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise PermissionError(13, "sharing violation")
                return real(self, *args, **kwargs)

            with mock.patch.object(Path, "read_text", flaky):
                data = procs.read_lock(lock)
            self.assertEqual(data.get("pid"), 1, "重试后应该读到内容")
            self.assertGreaterEqual(calls["n"], 4, "应该重试过")

    def test_persistent_error_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "worker.lock"
            lock.write_text("{}", encoding="utf-8")

            def always(self, *args, **kwargs):
                raise PermissionError(13, "sharing violation")

            started = time.time()
            with mock.patch.object(Path, "read_text", always):
                data = procs.read_lock(lock)
            self.assertEqual(data, {})
            self.assertLess(time.time() - started, 1.0, "重试不能把调用方拖住")

    def test_broken_json_returns_empty_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "worker.lock"
            lock.write_text("{not json", encoding="utf-8")
            self.assertEqual(procs.read_lock(lock), {})

class ConcurrentBeatTests(unittest.TestCase):
    """心跳线程和主循环都会 beat：并发写不能把锁文件写坏。"""

    def test_concurrent_beats_keep_file_parseable(self) -> None:
        import threading as _threading

        with tempfile.TemporaryDirectory() as tmp:
            saved = (worker.LOCK, worker.ROOM)
            worker.LOCK = Path(tmp) / "worker.lock"
            worker.ROOM = Path(tmp)
            try:
                errors = []
                stop = _threading.Event()

                def writer() -> None:
                    for _ in range(60):
                        if stop.is_set():
                            return
                        worker.beat()

                def reader() -> None:
                    for _ in range(120):
                        data = procs.read_lock(worker.LOCK)
                        # 允许读到空（替换瞬间），但不允许读到半截
                        if data and data.get("pid") != os.getpid():
                            errors.append(data)

                threads = [_threading.Thread(target=writer) for _ in range(4)]
                threads.append(_threading.Thread(target=reader))
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
                stop.set()
                self.assertEqual(errors, [], "并发写不能写出别人的 pid")
                final = procs.read_lock(worker.LOCK)
                self.assertEqual(final.get("pid"), os.getpid())
            finally:
                worker.LOCK, worker.ROOM = saved
