"""并行分析 + 多任务拆分的单元测试（不花真实模型的钱）。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_team import analysis, worker


class FakeCodex:
    """假 Codex：睡一会儿再返回一段分析文本。"""

    def __init__(self, delay: float = 0.3, fail: set[str] | None = None) -> None:
        self.delay = delay
        self.fail = fail or set()
        self.calls: list[str] = []

    def __call__(self, prompt: str, workspace: str | None = None, out_prefix: str | None = None,
                 sandbox: str = "workspace-write", timeout: int = 1200, model: str = "",
                 reasoning_effort: str = "") -> dict:
        kind = "risk" if "复核者" in prompt else "verify"
        self.calls.append(kind)
        time.sleep(self.delay)
        if kind in self.fail:
            raise RuntimeError("codex 挂了")
        path = Path(str(out_prefix) + ".last.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        label = "风险点：别动 a.py" if kind == "risk" else "验收命令：python -m unittest"
        path.write_text(label, encoding="utf-8")
        return {"ok": True, "last": str(path)}


class ParallelAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_both_analyses_run_and_are_faster_than_serial(self) -> None:
        fake = FakeCodex(delay=0.4)
        with patch.object(analysis, "run_codex", fake):
            started = time.time()
            result = analysis.analyze("加个函数", self.ws, self.root / "out")
            wall = time.time() - started
        self.assertTrue(result["risk"]["ok"])
        self.assertTrue(result["verify"]["ok"])
        self.assertIn("风险点", result["risk"]["text"])
        self.assertIn("验收命令", result["verify"]["text"])
        self.assertLess(wall, 0.4 * 2 * 0.85, "两份分析应该并行，墙钟时间不该是两者相加")
        self.assertGreaterEqual(result["serial"], 0.7)
        self.assertEqual(sorted(fake.calls), ["risk", "verify"])

    def test_one_failure_does_not_kill_the_other(self) -> None:
        fake = FakeCodex(delay=0.1, fail={"risk"})
        with patch.object(analysis, "run_codex", fake):
            result = analysis.analyze("加个函数", self.ws, self.root / "out")
        self.assertFalse(result["risk"]["ok"])
        self.assertIn("codex 挂了", result["risk"]["error"])
        self.assertTrue(result["verify"]["ok"], "一份分析失败不能连累另一份")

    def test_notes_builder_skips_empty(self) -> None:
        notes, _ = analysis.plan_notes({"risk": {"text": "别动 a.py"}, "verify": {"text": ""}})
        self.assertIn("预审风险", notes)
        self.assertNotIn("验收口径", notes)

    def test_notes_trim_long_text(self) -> None:
        notes, full = analysis.plan_notes({"risk": {"text": "行\n" * 500}, "verify": {"text": ""}})
        self.assertLessEqual(len(notes), 700)
        self.assertIn("…", notes)


class CardOrderTests(unittest.TestCase):
    """多任务拆分后的执行顺序。"""

    def test_single_card(self) -> None:
        cards = [{"title": "a"}]
        self.assertEqual(worker.order_cards(cards), cards)

    def test_declared_order_kept(self) -> None:
        cards = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
        self.assertEqual([c["title"] for c in worker.order_cards(cards)], ["a", "b", "c"])

    def test_dependency_pulls_task_forward(self) -> None:
        # b 声明在前，但它依赖第 2 个（a），所以 a 要先跑
        cards = [{"title": "b", "depends_on": [2]}, {"title": "a"}]
        self.assertEqual([c["title"] for c in worker.order_cards(cards)], ["a", "b"])

    def test_cycle_falls_back_to_declared_order(self) -> None:
        cards = [{"title": "a", "depends_on": [2]}, {"title": "b", "depends_on": [1]}]
        self.assertEqual([c["title"] for c in worker.order_cards(cards)], ["a", "b"])

    def test_unknown_dependency_ignored(self) -> None:
        cards = [{"title": "a", "depends_on": [9]}]
        self.assertEqual([c["title"] for c in worker.order_cards(cards)], ["a"])


class SplitPlanTests(unittest.TestCase):
    """大脑给的 tasks 数组要能规范成任务卡列表。"""

    def test_single_task(self) -> None:
        cards = worker.split_cards({"task": {"title": "a", "acceptance": ["x"], "scope": ["f.py"]}})
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "a")

    def test_tasks_array(self) -> None:
        cards = worker.split_cards({"tasks": [
            {"title": "a", "acceptance": ["x"], "scope": ["f.py"]},
            {"title": "b", "acceptance": ["y"], "scope": ["g.py"], "depends_on": [1]},
        ]})
        self.assertEqual([c["title"] for c in cards], ["a", "b"])
        self.assertEqual(cards[1]["depends_on"], [1])

    def test_tasks_array_wins_over_task(self) -> None:
        cards = worker.split_cards({
            "task": {"title": "旧", "acceptance": ["x"], "scope": ["f.py"]},
            "tasks": [{"title": "a", "acceptance": ["x"], "scope": ["f.py"]},
                      {"title": "b", "acceptance": ["y"], "scope": ["g.py"]}],
        })
        self.assertEqual([c["title"] for c in cards], ["a", "b"])

    def test_broken_entries_dropped(self) -> None:
        cards = worker.split_cards({"tasks": [
            {"title": "a", "acceptance": ["x"], "scope": ["f.py"]},
            {"title": "", "acceptance": [], "scope": []},
            "不是对象",
        ]})
        self.assertEqual([c["title"] for c in cards], ["a"])

    def test_empty_plan_gives_no_cards(self) -> None:
        self.assertEqual(worker.split_cards({}), [])


class RunPlanTests(unittest.TestCase):
    """一次需求拆成多步时的执行编排。"""

    def setUp(self) -> None:
        self.said: list[str] = []
        self.ran: list[str] = []
        self._say = worker.say
        self._run_card = worker.run_card
        worker.say = lambda text, **kw: self.said.append(text)

    def tearDown(self) -> None:
        worker.say = self._say
        worker.run_card = self._run_card

    def test_runs_in_dependency_order(self) -> None:
        def fake(card, ws, req, prescan=None, step_label=""):
            self.ran.append(card["title"])
            return True

        worker.run_card = fake
        cards = [{"title": "b", "depends_on": [2]}, {"title": "a"}]
        worker.run_plan(cards, Path("."), "两个改动")
        self.assertEqual(self.ran, ["a", "b"])
        self.assertTrue(any("拆成 2 步" in item for item in self.said))

    def test_stops_after_failed_step(self) -> None:
        def fake(card, ws, req, prescan=None, step_label=""):
            self.ran.append(card["title"])
            return card["title"] == "a"

        worker.run_card = fake
        cards = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
        worker.run_plan(cards, Path("."), "三个改动")
        self.assertEqual(self.ran, ["a", "b"], "第一步过了、第二步没过，第三步不该再跑")
        self.assertTrue(any("第 2 步" in item and "没过" in item for item in self.said))

    def test_all_passed_says_done(self) -> None:
        worker.run_card = lambda card, ws, req, prescan=None, step_label="": True
        worker.run_plan([{"title": "a"}, {"title": "b"}], Path("."), "两件事")
        self.assertTrue(any("全部做完" in item for item in self.said))


if __name__ == "__main__":
    unittest.main()