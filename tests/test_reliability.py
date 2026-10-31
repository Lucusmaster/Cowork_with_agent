"""可靠性回归：验收命令不得走 shell、scope 用真实 diff 核对、失败不得进成功终态。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_team import team, worker
from agent_team.winproc import quiet_flags


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                   encoding="utf-8", errors="replace", creationflags=quiet_flags(), check=False)


class VerifyCommandTests(unittest.TestCase):
    """验收命令必须变成 argv，任何 shell 元字符都要被拦下。"""

    def test_plain_command_is_split(self) -> None:
        argv = team.parse_verify_argv("python -m unittest discover -s tests -t .")
        self.assertEqual(argv, ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."])

    def test_list_passthrough(self) -> None:
        self.assertEqual(team.parse_verify_argv(["python", "-m", "unittest"]),
                         ["python", "-m", "unittest"])

    def test_json_array(self) -> None:
        self.assertEqual(team.parse_verify_argv('["python", "-m", "pytest"]'),
                         ["python", "-m", "pytest"])

    def test_shell_metacharacters_rejected(self) -> None:
        for command in ("cat a | grep b", "echo hi > out.txt", "rm -rf / ; echo x",
                        "echo $(whoami)", "echo `id`"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                team.parse_verify_argv(command)

    def test_empty_command_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            team.parse_verify_argv("   ")


class ScopeCheckTests(unittest.TestCase):
    """scope 必须拿真实改动核对，不能只看执行者的自述。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        git(self.ws, "init")
        git(self.ws, "config", "user.email", "t@example.com")
        git(self.ws, "config", "user.name", "t")
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-m", "init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clean_tree_has_no_violation(self) -> None:
        self.assertEqual(team.scope_violations({"scope": ["src/"]}, self.ws), [])

    def test_out_of_scope_file_is_reported(self) -> None:
        (self.ws / "README.md").write_text("hello\n", encoding="utf-8")
        bad = team.scope_violations({"scope": ["src/"]}, self.ws)
        self.assertEqual(bad, ["README.md"])

    def test_in_scope_change_is_allowed(self) -> None:
        (self.ws / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
        self.assertEqual(team.scope_violations({"scope": ["src/"]}, self.ws), [])

    def test_cache_and_build_artifacts_are_not_violations(self) -> None:
        (self.ws / "__pycache__").mkdir()
        (self.ws / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"x")
        (self.ws / ".pytest_cache").mkdir()
        (self.ws / ".pytest_cache" / "v").write_text("x", encoding="utf-8")
        self.assertEqual(team.scope_violations({"scope": ["src/"]}, self.ws), [])

    def test_real_out_of_scope_change_still_caught(self) -> None:
        (self.ws / "__pycache__").mkdir()
        (self.ws / "__pycache__" / "x.pyc").write_bytes(b"x")
        (self.ws / "secret.env").write_text("k=v", encoding="utf-8")
        self.assertEqual(team.scope_violations({"scope": ["src/"]}, self.ws), ["secret.env"])

    def test_empty_scope_means_no_restriction(self) -> None:
        (self.ws / "anything.txt").write_text("a\n", encoding="utf-8")
        self.assertEqual(team.scope_violations({"scope": []}, self.ws), [])



class CollabStrayTests(unittest.TestCase):
    """协作仓库里的改动：只有命中任务 scope 才算串味。"""

    def _stray(self, scope, changed):
        from unittest.mock import patch
        with patch.object(team, "ledger_snapshot", return_value=dict(changed)):
            return team.collab_stray_changes({}, scope)

    def test_developer_edits_not_flagged_when_scope_given(self) -> None:
        got = self._stray(["textstats.py"], {"agent_team/worker.py": "abc"})
        self.assertEqual(got, [], "开发者改自己的代码不该被判失败")

    def test_executor_writing_scope_into_collab_repo_is_flagged(self) -> None:
        got = self._stray(["textstats.py", "tests"], {"textstats.py": "abc"})
        self.assertEqual(got, ["textstats.py"])

    def test_scope_dir_match(self) -> None:
        got = self._stray(["tests"], {"tests/test_textstats.py": "abc"})
        self.assertEqual(got, ["tests/test_textstats.py"])

    def test_without_scope_keeps_old_strict_behavior(self) -> None:
        got = self._stray([], {"agent_team/worker.py": "abc"})
        self.assertEqual(got, ["agent_team/worker.py"])


class FinishedOutcomeTests(unittest.TestCase):
    """失败的任务不能因为流程走完就标记成功。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_dir = self.root / "tasks" / "T-260101-000001"
        self.task_dir.mkdir(parents=True)
        self.original = worker.TASKS
        worker.TASKS = self.root / "tasks"

    def tearDown(self) -> None:
        worker.TASKS = self.original
        self.tmp.cleanup()

    def _write(self, task: dict) -> None:
        (self.task_dir / "task.json").write_text(json.dumps(task, ensure_ascii=False),
                                                 encoding="utf-8")

    def _read(self) -> dict:
        return json.loads((self.task_dir / "task.json").read_text(encoding="utf-8"))

    def test_passed_and_review_ok_is_success(self) -> None:
        self._write({"id": "T-260101-000001", "status": "passed",
                     "reviews": [{"verdict": "PASS"}]})
        worker.mark_finished("T-260101-000001")
        task = self._read()
        self.assertEqual(task["outcome"], "success")
        self.assertIn("finished_at", task)

    def test_accepted_but_review_needs_fix_is_failure(self) -> None:
        self._write({"id": "T-260101-000001", "status": "passed",
                     "reviews": [{"verdict": "NEEDS_FIX"}]})
        worker.mark_finished("T-260101-000001")
        task = self._read()
        self.assertEqual(task["outcome"], "failure")
        self.assertEqual(task["status"], "failed")

    def test_failed_verify_is_failure(self) -> None:
        self._write({"id": "T-260101-000001", "status": "failed", "reviews": []})
        worker.mark_finished("T-260101-000001")
        self.assertEqual(self._read()["outcome"], "failure")

    def test_no_review_is_failure(self) -> None:
        self._write({"id": "T-260101-000001", "status": "passed", "reviews": []})
        worker.mark_finished("T-260101-000001")
        self.assertEqual(self._read()["outcome"], "failure")


if __name__ == "__main__":
    unittest.main()