"""验收命令要按任务类型来：静态文件直接检查产物，别再硬编成 unittest。

回归 T-261102-012458：空工作区里要做的是一个 HTML 文件，验收命令却是
`python -m unittest discover -s tests -t . -v`，两轮都卡在
`ImportError: Start directory is not importable: 'tests'`。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_team import analysis, team, worker


class PromptTests(unittest.TestCase):
    """提示词不能再把"优先 unittest"写死。"""

    def test_verify_prompt_knows_the_workspace(self) -> None:
        self.assertIn("{facts}", analysis.VERIFY_PROMPT)
        self.assertIn("python -c", analysis.VERIFY_PROMPT)
        self.assertNotIn("优先 `python -m unittest", analysis.VERIFY_PROMPT)

    def test_planner_prompts_are_task_aware(self) -> None:
        for name in ("PLANNER_PROMPT", "BRAIN_PROMPT"):
            text = getattr(worker, name)
            self.assertNotIn("优先 `python -m unittest", text, name)
            self.assertIn("python -c", text, name)

    def test_facts_describe_an_empty_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            facts = analysis.workspace_facts(Path(tmp))
        self.assertIn("空目录", facts)
        self.assertIn("没有测试目录", facts)

    def test_facts_describe_a_python_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "app.py").write_text("print(1)", encoding="utf-8")
            facts = analysis.workspace_facts(root)
        self.assertIn("已有 tests/", facts)
        self.assertIn("app.py", facts)


class VerifyArgvTests(unittest.TestCase):
    """`python -c "..."` 要能当一个参数传下去，引号外的元字符照样拦。"""

    def test_quoted_code_is_one_argument(self) -> None:
        cmd = 'python -c "import sys; sys.exit(0)"'
        self.assertEqual(team.parse_verify_argv(cmd),
                         ["python", "-c", "import sys; sys.exit(0)"])

    def test_nested_single_quotes_stay_in_the_code(self) -> None:
        cmd = ("python -c \"import pathlib, sys; p = pathlib.Path('a.html'); "
               "sys.exit(0 if p.exists() else 1)\"")
        argv = team.parse_verify_argv(cmd)
        self.assertEqual(argv[:2], ["python", "-c"])
        self.assertIn("'a.html'", argv[2])

    def test_unquoted_metacharacters_rejected(self) -> None:
        for command in ("cat a | grep b", "echo hi > out.txt", "rm -rf / ; echo x",
                        "echo $(whoami)", "echo `id`"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                team.parse_verify_argv(command)

    def test_unclosed_quote_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            team.parse_verify_argv('python -c "unterminated')


class SanitizeTests(unittest.TestCase):
    """白名单不能把合理的产物检查替换成 unittest 兜底。"""

    def test_artifact_check_survives(self) -> None:
        cmd = ("python -c \"import pathlib, sys; p = pathlib.Path('pelican.html'); "
               "sys.exit(0 if p.exists() and '<svg' in p.read_text(encoding='utf-8') else 1)\"")
        card = worker.sanitize_plan({
            "title": "做一个 HTML", "goal": "静态页", "acceptance": ["文件存在"],
            "verify": [cmd], "scope": ["pelican.html"],
        })
        self.assertEqual(card["verify"], [cmd])
        self.assertNotIn(worker.DEFAULT_VERIFY, card["verify"])

    def test_comparison_operator_is_not_mistaken_for_redirection(self) -> None:
        cmd = "python -c \"import sys; sys.exit(0 if len(sys.argv) > 0 else 1)\""
        card = worker.sanitize_plan({
            "title": "t", "goal": "g", "acceptance": ["a"], "verify": [cmd], "scope": ["a.html"],
        })
        self.assertEqual(card["verify"], [cmd])

    def test_unknown_command_still_falls_back(self) -> None:
        card = worker.sanitize_plan({
            "title": "t", "goal": "g", "acceptance": ["a"],
            "verify": ["curl http://example.com"], "scope": ["a.html"],
        })
        self.assertEqual(card["verify"], [worker.DEFAULT_VERIFY])


if __name__ == "__main__":
    unittest.main()