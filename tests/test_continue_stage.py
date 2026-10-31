"""「继续」必须接着上一阶段做：意图识别、上下文组装、阶段编号。

守的是这个真实踩坑：用户在看板上打完一阶段后输入「继续」，
引擎把它当成信息不足直接拒了——因为「继续」被写在 VAGUE 里。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 包在仓库根

from agent_team import worker  # noqa: E402


class ContinueIntentTests(unittest.TestCase):
    def test_plain_continue_words(self) -> None:
        for text in ("继续", "继续吧", "继续干", "下一步", "下一阶段", "接着做", "continue"):
            self.assertTrue(worker.is_continue_intent(text), text)

    def test_continue_with_remark_still_continues(self) -> None:
        for text in ("继续做示例项目的第二阶段", "接着把测试补上", "下一阶段做导出功能"):
            self.assertTrue(worker.is_continue_intent(text), text)

    def test_explicit_new_repo_is_a_new_task(self) -> None:
        for text in ("改 E:\\my-project 里的 README", "在 D:/work/foo 加日志"):
            self.assertFalse(worker.is_continue_intent(text), text)

    def test_ordinary_request_is_not_continue(self) -> None:
        self.assertFalse(worker.is_continue_intent("改一下 README 的标题"))


class StageContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.tasks = base / "tasks"
        self.room = base / "room"
        self.proj = base / "proj"
        for path in (self.tasks, self.room, self.proj):
            path.mkdir(parents=True, exist_ok=True)
        (self.proj / "main.py").write_text("print('hi')\n", encoding="utf-8")
        self._saved = (worker.TASKS, worker.ROOM, worker.ROOT)
        worker.TASKS, worker.ROOM, worker.ROOT = self.tasks, self.room, base

    def tearDown(self) -> None:
        worker.TASKS, worker.ROOM, worker.ROOT = self._saved
        self.tmp.cleanup()

    def _card(self, tid: str, stage: int, status: str = "passed", workspace: Path | None = None) -> dict:
        directory = self.tasks / tid
        directory.mkdir(parents=True, exist_ok=True)
        card = {
            "id": tid, "title": f"第{stage}阶段", "goal": "把某功能做完",
            "status": status, "stage": stage, "rounds": [],
            "workspace": str(workspace or self.proj),
            "created_at": "2026-10-31T10:00:00", "updated_at": "2026-10-31T10:30:00",
        }
        (directory / "task.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        return card

    def test_last_project_card_skips_ledger_tasks(self) -> None:
        self._card("T-000001", 1)
        ledger_dir = self.tasks / "T-000002"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "task.json").write_text(json.dumps({
            "id": "T-000002", "title": "账本自己的活", "status": "passed",
            "workspace": str(worker.ROOT), "updated_at": "2026-10-31T11:00:00",
        }, ensure_ascii=False), encoding="utf-8")

        picked = worker.last_project_card()
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "T-000001")

    def test_stage_number_increments_per_workspace(self) -> None:
        self._card("T-000001", 1)
        self._card("T-000003", 2)
        self.assertEqual(worker.stage_number(self.proj), 3)

    def test_context_carries_report_verify_and_review(self) -> None:
        card = self._card("T-000010", 1, status="failed")
        directory = self.tasks / "T-000010"
        (directory / "report.md").write_text("# 报告\n\n做完了登录，导出没做。\n", encoding="utf-8")
        (directory / "verify-01.txt").write_text("FAIL: test_export (1 failure)\n", encoding="utf-8")
        (directory / "review-01-0.last.md").write_text("VERDICT: NEEDS_FIX\n导出缺参数校验\n", encoding="utf-8")

        context = worker.build_continue_context(card)
        self.assertIn("导出没做", context)          # 上一阶段没做完的事
        self.assertIn("FAIL: test_export", context)  # 测试失败项
        self.assertIn("NEEDS_FIX", context)          # 复核意见
        self.assertIn("上一阶段任务 T-000010", context)

    def test_progress_file_written_after_stage(self) -> None:
        self._card("T-000020", 1)
        directory = self.tasks / "T-000020"
        (directory / "report.md").write_text("# 报告\n\n第一阶段完成。\n", encoding="utf-8")
        (directory / "review-01-0.last.md").write_text("VERDICT: PASS\n", encoding="utf-8")

        worker.append_progress("T-000020")
        target = worker.stage_progress_file(self.proj)
        self.assertTrue(target.is_file())
        text = target.read_text(encoding="utf-8")
        self.assertIn("第 1 阶段", text)
        self.assertIn("PASS", text)




class HandleContinueFlowTests(unittest.TestCase):
    """「继续」要能建出第 2 阶段的卡，并把交接材料写到卡上（不花钱、不调 Codex）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.tasks = base / "tasks"
        self.room = base / "room"
        self.proj = base / "proj"
        for path in (self.tasks, self.room, self.proj):
            path.mkdir(parents=True, exist_ok=True)
        self._saved = (worker.TASKS, worker.ROOM, worker.ROOT)
        worker.TASKS, worker.ROOM, worker.ROOT = self.tasks, self.room, base

        prev = self.tasks / "T-260101-100001"
        prev.mkdir()
        (prev / "task.json").write_text(json.dumps({
            "id": "T-260101-100001", "title": "第一阶段", "goal": "先做登录", "status": "passed",
            "stage": 1, "rounds": [], "workspace": str(self.proj),
            "created_at": "2026-10-31T10:00:00", "updated_at": "2026-10-31T10:30:00",
        }, ensure_ascii=False), encoding="utf-8")
        (prev / "report.md").write_text("# 报告\n登录完成，导出没做。\n", encoding="utf-8")
        (prev / "review-01-0.last.md").write_text("VERDICT: PASS\n", encoding="utf-8")

        self.made: list[str] = []
        self.pipeline_calls: list[str] = []

        def fake_plan(request, context, workspace):
            self.context_seen = context
            return {
                "title": "补导出功能",
                "goal": "把导出补上，覆盖上一阶段没做的部分",
                "acceptance": ["导出能把数据写成 csv"],
                "verify": ["python -m unittest discover -s tests -t . -v"],
                "scope": ["export.py"],
                "unfinished_after": ["打包安装脚本"],
            }

        def fake_run_step(args, timeout, label):
            if args and args[0] == "new":
                tid = "T-260101-100002"
                directory = self.tasks / tid
                directory.mkdir(parents=True, exist_ok=True)
                card = {"id": tid, "title": "", "goal": "", "status": "planned",
                        "stage": 1, "rounds": [], "workspace": str(self.proj)}
                for flag, key in (("--title", "title"), ("--goal", "goal")):
                    if flag in args:
                        card[key] = args[args.index(flag) + 1]
                (directory / "task.json").write_text(json.dumps(card, ensure_ascii=False),
                                                     encoding="utf-8")
                self.made.append(tid)
                return 0, f"已建卡 {tid}"
            return 0, ""

        self._patch = (worker.plan_continue_with_codex, worker.run_step, worker.pipeline)
        worker.plan_continue_with_codex = fake_plan
        worker.run_step = fake_run_step
        worker.pipeline = lambda tid, card, request: self.pipeline_calls.append(tid)

    def tearDown(self) -> None:
        worker.plan_continue_with_codex, worker.run_step, worker.pipeline = self._patch
        worker.TASKS, worker.ROOM, worker.ROOT = self._saved
        self.tmp.cleanup()

    def test_continue_creates_next_stage_with_handoff(self) -> None:
        worker.handle_continue("继续")

        self.assertEqual(self.made, ["T-260101-100002"])
        self.assertEqual(self.pipeline_calls, ["T-260101-100002"])

        card = json.loads((self.tasks / "T-260101-100002" / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(card["stage"], 2)
        self.assertEqual(card["continues"], "T-260101-100001")
        self.assertEqual(card["unfinished_after"], ["打包安装脚本"])

        handoff = (self.tasks / "T-260101-100002" / "handoff.md").read_text(encoding="utf-8")
        self.assertIn("T-260101-100001", handoff)
        self.assertIn("导出没做", handoff)              # 上一阶段报告内容带过来了
        self.assertIn("本阶段规划依据", handoff)

        plan_md = (self.tasks / "T-260101-100002" / "plan.md").read_text(encoding="utf-8")
        self.assertIn("第 2 阶段", plan_md)

    def test_continue_when_nothing_to_do(self) -> None:
        worker.plan_continue_with_codex = lambda request, context, workspace: {
            "done": True, "reason": "报告里的未完成项已经清零"}
        self.said: list[str] = []
        worker.say = self.said.append
        worker.handle_continue("继续")
        self.assertEqual(self.made, [])
        self.assertTrue(any("没有未完成项" in text for text in self.said))


if __name__ == "__main__":
    unittest.main()