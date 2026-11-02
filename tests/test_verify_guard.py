"""验收命令体检的测试：分清"代码没写对"和"验收脚本自己坏了"。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from agent_team import team, verify_guard, worker

PASS_CMD = f'"{sys.executable}" -c "print(1)"'
CRASH_CMD = f'"{sys.executable}" -c "import re; re.search(chr(120),chr(97)).group(1)"'
ASSERT_CMD = f'"{sys.executable}" -c "assert 1==2, chr(120)"'
MISSING_CMD = f'"{sys.executable}" -c "open(chr(110)+chr(111))"'


class CrashReasonTest(unittest.TestCase):
    def test_attribute_error_is_script_fault(self) -> None:
        out = ('Traceback (most recent call last):\n  File "<string>", line 1, in <module>\n'
               "AttributeError: 'NoneType' object has no attribute 'group'")
        self.assertEqual(verify_guard.crash_reason(out), "AttributeError")

    def test_assertion_failure_is_not_script_fault(self) -> None:
        out = ("Traceback (most recent call last):\n  File \"<string>\", line 1\n"
               "AssertionError: 28.075 not in range")
        self.assertEqual(verify_guard.crash_reason(out), "")

    def test_missing_artifact_is_not_script_fault(self) -> None:
        out = ("Traceback (most recent call last):\n  File \"<string>\", line 1, in <module>\n"
               "FileNotFoundError: [Errno 2] No such file or directory: 'page.html'")
        self.assertEqual(verify_guard.crash_reason(out), "")

    def test_syntax_error_is_script_fault(self) -> None:
        out = ('  File "<string>", line 9\nSyntaxError: (\' was never closed')
        self.assertEqual(verify_guard.crash_reason(out), "")
        out = 'Traceback (most recent call last):\nSyntaxError: \'(\' was never closed'
        self.assertEqual(verify_guard.crash_reason(out), "SyntaxError")

    def test_plain_text_is_not_a_crash(self) -> None:
        self.assertEqual(verify_guard.crash_reason(""), "")
        self.assertEqual(verify_guard.crash_reason("ok\n"), "")


class ProbeTest(unittest.TestCase):
    def test_probe_finds_crashing_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = verify_guard.probe_all(
                [("crash", [sys.executable, "-c", "import re; re.search(chr(120), chr(97)).group(1)"]),
                 ("pass", [sys.executable, "-c", "print(1)"])],
                cwd=tmp)
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["command"], "crash")
        self.assertEqual(broken[0]["reason"], "AttributeError")

    def test_probe_ignores_assertion_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = verify_guard.probe_all(
                [("assert", [sys.executable, "-c", "assert 1 == 2"]),
                 ("missing", [sys.executable, "-c", "open('nope-xyz.txt')"])],
                cwd=tmp)
        self.assertEqual(broken, [])

    def test_render_broken_says_not_to_rework_executor(self) -> None:
        text = verify_guard.render_broken(
            [{"command": "python -c 'x()'", "reason": "AttributeError", "code": 1,
              "seconds": 0, "output": "AttributeError: ..."}])
        self.assertIn("验收命令自身报错", text)
        self.assertIn("不要给执行者派返工单", text)


class PreflightTest(unittest.TestCase):
    def test_preflight_writes_marker_only_when_script_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp) / "task"
            tdir.mkdir()
            bad_task = {"id": "T-pre", "workspace": tmp,
                        "verify": ["python -c 'import re; re.search(1, 2).group(1)'"]}
            broken = team.preflight_verify_scripts(bad_task, tdir)
            self.assertTrue(broken, "崩溃的命令应该被拦下")
            self.assertTrue(verify_guard.marker_path(tdir).is_file())

            good_task = {"id": "T-pre2", "workspace": tmp, "verify": ["python -c 'print(1)'"]}
            tdir2 = Path(tmp) / "task2"
            tdir2.mkdir()
            self.assertEqual(team.preflight_verify_scripts(good_task, tdir2), [])
            self.assertFalse(verify_guard.marker_path(tdir2).exists())

    def test_worker_reads_marker(self) -> None:
        import shutil

        tid = "T-marker-probe"
        tdir = worker.TASKS / tid
        shutil.rmtree(tdir, ignore_errors=True)
        tdir.mkdir(parents=True, exist_ok=True)
        try:
            self.assertEqual(worker._verify_script_broken(tid), "")
            (tdir / "verify-script-broken.md").write_text("# 验收命令自身报错\n", encoding="utf-8")
            self.assertIn("验收命令自身报错", worker._verify_script_broken(tid))
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()