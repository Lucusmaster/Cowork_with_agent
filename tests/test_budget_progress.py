"""预算闸门与进度心跳的测试。"""

from __future__ import annotations

import time
import unittest

from agent_team import budget, progress


class BudgetTest(unittest.TestCase):
    def test_usage_sums_rounds(self) -> None:
        task = {"rounds": [{"tokens": 1200, "seconds": 60}, {"tokens": 800, "seconds": 40}]}
        used = budget.usage(task)
        self.assertEqual(used["tokens"], 2000)
        self.assertEqual(used["seconds"], 100)
        self.assertEqual(used["rounds"], 2)

    def test_usage_tolerates_garbage(self) -> None:
        task = {"rounds": [{"tokens": "abc"}, {"tokens": None}, {}]}
        self.assertEqual(budget.usage(task)["tokens"], 0)

    def test_check_respects_configured_limits(self) -> None:
        caps = budget.limits()
        self.assertGreaterEqual(caps["max_tokens"], 1)
        # 卡在默认上限之内，不该报警
        self.assertEqual(budget.check({"rounds": [{"tokens": 1000, "seconds": 10}]}), "")
        # 超 token 上限
        over_tokens = {"rounds": [{"tokens": caps["max_tokens"], "seconds": 1}]}
        self.assertIn("tokens", budget.check(over_tokens))
        # 超时间上限
        over_seconds = {"rounds": [{"tokens": 1, "seconds": caps["max_seconds"]}]}
        self.assertIn("分钟", budget.check(over_seconds))

    def test_note_mentions_usage_and_cap(self) -> None:
        text = budget.note({"rounds": [{"tokens": 5000, "seconds": 90}]})
        self.assertIn("5,000", text)
        self.assertIn("上限", text)


class ProgressTest(unittest.TestCase):
    def test_tail_line_skips_blank_lines(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.err.txt"
            path.write_text("第一行\n\n最后一行\n\n", encoding="utf-8")
            self.assertEqual(progress.tail_line(path), "最后一行")

    def test_tail_line_missing_file_is_empty(self) -> None:
        self.assertEqual(progress.tail_line("C:/definitely/not/here.txt"), "")

    def test_newest_file_picks_latest(self) -> None:
        import tempfile
        import time as _time
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "exec-01.err.txt"
            old.write_text("old", encoding="utf-8")
            _time.sleep(0.02)
            new = Path(tmp) / "exec-01-attempt1.err.txt"
            new.write_text("new", encoding="utf-8")
            self.assertEqual(progress.newest_file(tmp, "exec-01*err.txt"), new)

    def test_beater_emits_then_stops(self) -> None:
        seen: list[str] = []
        beater = progress.Beater("第 1 轮执行", interval=0.05, emit=seen.append).start()
        time.sleep(0.22)
        beater.stop()
        self.assertGreaterEqual(beater.beats, 2)
        self.assertTrue(seen)
        self.assertIn("已跑", seen[0])
        after = len(seen)
        time.sleep(0.15)
        self.assertEqual(len(seen), after, "stop() 之后不该再发心跳")

    def test_beater_includes_log_tail(self) -> None:
        import tempfile
        from pathlib import Path

        seen: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "exec-01.err.txt").write_text("running tests...\n", encoding="utf-8")
            with progress.Beater("第 1 轮执行", directory=tmp, pattern="exec-01*err.txt",
                                 interval=0.05, emit=seen.append):
                time.sleep(0.18)
        self.assertTrue(any("running tests" in text for text in seen), seen)

    def test_human_seconds(self) -> None:
        self.assertEqual(progress.human_seconds(9), "9s")
        self.assertEqual(progress.human_seconds(75), "1m15s")


if __name__ == "__main__":
    unittest.main()