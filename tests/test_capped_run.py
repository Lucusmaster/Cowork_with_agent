"""硬超时：`subprocess.run(timeout=)` 在 Windows 上会被孙进程拖死，`run_capped` 不会。

背景（2026-11-02 实测）：执行者的 `codex exec` 超时 900s 后，`subprocess.run` 先
kill 直接子进程、再 `communicate()` 收尾——而收尾这一步没有超时；Codex 留下的
孙进程还握着 stdout 管道，于是 dispatch 又挂了 20 分钟，界面上就是"卡住不动"。
`run_capped` 在超时时连进程树一起杀（`taskkill /T`），再最多等 10s 收尾。
"""
from __future__ import annotations

import sys
import time
import unittest

from agent_team.winproc import run_capped

# 父进程再起一个孙子进程，让孙子继承 stdout 管道，然后自己慢慢睡
HOLDER = r'''
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print("started", flush=True)
time.sleep(120)
'''


class CappedRunTests(unittest.TestCase):
    def test_timeout_returns_even_when_grandchild_holds_pipe(self) -> None:
        started = time.time()
        rc, out, err, timedout = run_capped([sys.executable, "-c", HOLDER], timeout=1)
        elapsed = time.time() - started
        self.assertTrue(timedout)
        self.assertEqual(rc, 124)
        # 关键：必须在"超时 + 收尾"之后几秒内返回，而不是等孙子进程睡完
        self.assertLess(elapsed, 25, f"超时后 {elapsed:.1f}s 才返回，说明又被孙进程拖住了")

    def test_normal_command_returns_output(self) -> None:
        rc, out, err, timedout = run_capped([sys.executable, "-c", "print('hi')"], timeout=30)
        self.assertEqual(rc, 0)
        self.assertFalse(timedout)
        self.assertIn("hi", out)

    def test_input_text_reaches_child(self) -> None:
        rc, out, _, _ = run_capped(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().strip().upper())"],
            input_text="abc", timeout=30)
        self.assertEqual(rc, 0)
        self.assertIn("ABC", out)

    def test_missing_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            run_capped([sys.executable, "-c", "print(1)"])


if __name__ == "__main__":
    unittest.main()