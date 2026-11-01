"""可靠性回归：验收命令不得走 shell、scope 用真实 diff 核对、失败不得进成功终态。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
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


class EarlyScopeReportTests(unittest.TestCase):
    """执行完立刻核对 scope：越界当场说出来，别等到验收（那已经烧完一整轮了）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        git(self.ws, "init")
        git(self.ws, "config", "user.email", "t@example.com")
        git(self.ws, "config", "user.name", "t")
        (self.ws / "keep.txt").write_text("k\n", encoding="utf-8")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-m", "init")

        self.tasks = Path(self.tmp.name) / "tasks"
        self.tasks.mkdir()
        self._saved = {"TASKS": team.TASKS}
        team.TASKS = self.tasks
        self.events: list[str] = []
        self._saved_room = team.room_event
        team.room_event = lambda text, *a, **k: self.events.append(text)

    def tearDown(self) -> None:
        team.TASKS = self._saved["TASKS"]
        team.room_event = self._saved_room
        self.tmp.cleanup()

    def _card(self, scope: list[str]) -> dict:
        tid = "T-990101-000001"
        (self.tasks / tid).mkdir(parents=True, exist_ok=True)
        task = {"id": tid, "scope": scope, "workspace": str(self.ws),
                "title": "t", "goal": "g", "status": "executed"}
        (self.tasks / tid / "task.json").write_text(
            json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return task

    def test_clean_round_reports_nothing(self) -> None:
        task = self._card(["keep.txt"])
        self.assertEqual(team.report_scope_now(task, 1), [])
        self.assertEqual(self.events, [])

    def test_out_of_scope_change_is_reported_immediately(self) -> None:
        task = self._card(["keep.txt"])
        (self.ws / "oops.txt").write_text("x\n", encoding="utf-8")
        got = team.report_scope_now(task, 1)
        self.assertEqual(got, ["oops.txt"])
        self.assertTrue(any("越界" in e for e in self.events),
                        f"越界要当场发一条事件，实际 {self.events}")
        saved = json.loads((self.tasks / task["id"] / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["out_of_scope"], ["oops.txt"], "越界要落进任务卡，右栏才看得到")

    def test_marker_clears_once_fixed(self) -> None:
        task = self._card(["keep.txt"])
        stray = self.ws / "oops.txt"
        stray.write_text("x\n", encoding="utf-8")
        self.assertEqual(team.report_scope_now(task, 1), ["oops.txt"])
        stray.unlink()
        self.assertEqual(team.report_scope_now(task, 2), [])
        saved = json.loads((self.tasks / task["id"] / "task.json").read_text(encoding="utf-8"))
        self.assertNotIn("out_of_scope", saved, "改回来了就别再挂着越界标记")

    def test_broken_workspace_does_not_break_dispatch(self) -> None:
        task = self._card(["keep.txt"])
        task["workspace"] = str(Path(self.tmp.name) / "nope")
        self.assertEqual(team.report_scope_now(task, 1), [])


class TaskIdCollisionTests(unittest.TestCase):
    """同一秒建两张卡不能互相覆盖（原来会静默盖掉前一张）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = team.TASKS
        team.TASKS = Path(self.tmp.name) / "tasks"
        team.TASKS.mkdir()

    def tearDown(self) -> None:
        team.TASKS = self._saved
        self.tmp.cleanup()

    def test_first_card_keeps_plain_stamp(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5)
        self.assertEqual(team.new_task_id(now), "T-300102-030405")

    def test_same_second_gets_suffix_instead_of_overwriting(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5)
        first = team.new_task_id(now)
        (team.TASKS / first).mkdir()
        second = team.new_task_id(now)
        (team.TASKS / second).mkdir()
        third = team.new_task_id(now)
        self.assertEqual([first, second, third],
                         ["T-300102-030405", "T-300102-030405-2", "T-300102-030405-3"])

    def test_ids_stay_unique_under_a_burst(self) -> None:
        now = datetime(2030, 1, 2, 3, 4, 5)
        made = []
        for _ in range(20):
            tid = team.new_task_id(now)
            (team.TASKS / tid).mkdir()
            made.append(tid)
        self.assertEqual(len(set(made)), 20, f"ID 撞了：{made}")



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



class ReconcileTests(unittest.TestCase):
    """引擎重启后，跑完但没标终态的任务要补上，否则等结果的一方永远等下去。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original = worker.TASKS
        worker.TASKS = self.root / "tasks"
        (worker.TASKS).mkdir(parents=True)

    def tearDown(self) -> None:
        worker.TASKS = self.original
        self.tmp.cleanup()

    def _card(self, tid: str, **fields) -> Path:
        d = worker.TASKS / tid
        d.mkdir(parents=True)
        data = {"id": tid, "status": "passed", "reviews": [{"verdict": "PASS"}]}
        data.update(fields)
        (d / "task.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return d

    def test_finished_task_gets_outcome(self) -> None:
        self._card("T-260101-000001")
        self.assertEqual(worker.reconcile_unfinished(), ["T-260101-000001"])
        data = json.loads((worker.TASKS / "T-260101-000001" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(data["outcome"], "success")
        self.assertIn("finished_at", data)

    def test_already_finished_is_skipped(self) -> None:
        self._card("T-260101-000002", finished_at="2026-01-01T00:00:00", outcome="success")
        self.assertEqual(worker.reconcile_unfinished(), [])

    def test_running_task_is_left_alone(self) -> None:
        self._card("T-260101-000003", status="executed")
        self.assertEqual(worker.reconcile_unfinished(), [])
        data = json.loads((worker.TASKS / "T-260101-000003" / "task.json").read_text(encoding="utf-8"))
        self.assertNotIn("finished_at", data)

    def test_failed_task_marked_failure(self) -> None:
        self._card("T-260101-000004", status="failed", reviews=[])
        worker.reconcile_unfinished()
        data = json.loads((worker.TASKS / "T-260101-000004" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(data["outcome"], "failure")


if __name__ == "__main__":
    unittest.main()