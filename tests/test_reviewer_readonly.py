"""复核者只读：这是架构级约束，不能靠"这轮我没改"的自觉。

背景：一份外部审核报告的作者有写盘权限，它顺手往仓库提交了一个修复（`af4ab68`）。
修得对，但**权限模型错了**——复核者一旦能写，"决策与执行分离"就是句空话。
我们自己的复核者之所以可信，是因为 `--sandbox read-only` 从技术上做不到写。
这组测试就是钉住"技术上做不到"，外加一道事后对账（真写了也要被抓出来）。
"""
from __future__ import annotations

import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_team import team


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          encoding="utf-8", errors="replace")
    return proc.stdout or ""


class ReviewSandboxTests(unittest.TestCase):
    """沙箱请求一律降级成只读，并且留痕（静默改等于没有审计）。"""

    def setUp(self) -> None:
        self.events: list[str] = []
        self._saved = team.room_event
        team.room_event = lambda text, *a, **k: self.events.append(text)

    def tearDown(self) -> None:
        team.room_event = self._saved

    def test_writable_request_is_clamped(self) -> None:
        self.assertEqual(team._review_sandbox("workspace-write"), "read-only")

    def test_full_access_request_is_clamped(self) -> None:
        self.assertEqual(team._review_sandbox("danger-full-access"), "read-only")

    def test_clamp_is_audited(self) -> None:
        team._review_sandbox("workspace-write", "T-1")
        self.assertTrue(any("强制降为只读" in e for e in self.events),
                        f"降级必须留一条事件，实际 {self.events}")

    def test_read_only_request_is_quiet(self) -> None:
        team._review_sandbox("read-only", "T-1")
        self.assertEqual(self.events, [], "本来就只读就别刷告警")

    def test_empty_defaults_to_read_only(self) -> None:
        self.assertEqual(team._review_sandbox(None), "read-only")
        self.assertEqual(team._review_sandbox(""), "read-only")

    def test_cmd_review_uses_the_clamp(self) -> None:
        """源码级保证：cmd_review 不能再直接 `args.sandbox or cfg[...]`。"""
        src = inspect.getsource(team.cmd_review)
        self.assertIn("_review_sandbox(", src, "复核必须走降级函数")
        self.assertNotIn('sandbox = args.sandbox or cfg["sandbox"]', src,
                         "直接吃配置/命令行参数 = 默认只读但可以被改掉")


class ReviewWriteAuditTests(unittest.TestCase):
    """事后对账：就算沙箱哪天失效，复核动了文件也要被当场抓出来。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        git(self.ws, "init")
        git(self.ws, "config", "user.email", "t@example.com")
        git(self.ws, "config", "user.name", "t")
        (self.ws / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-m", "init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clean_workspace_fingerprint_is_empty(self) -> None:
        self.assertEqual(team.workspace_fingerprint(self.ws), {})

    def test_modified_file_shows_up(self) -> None:
        (self.ws / "app.py").write_text("x = 2\n", encoding="utf-8")
        before = team.workspace_fingerprint(self.ws)
        (self.ws / "app.py").write_text("x = 3\n", encoding="utf-8")
        after = team.workspace_fingerprint(self.ws)
        self.assertEqual(team._diff_fingerprint(before, after), ["app.py"])

    def test_new_file_shows_up(self) -> None:
        before = team.workspace_fingerprint(self.ws)
        (self.ws / "sneaky.py").write_text("print(1)\n", encoding="utf-8")
        after = team.workspace_fingerprint(self.ws)
        self.assertEqual(team._diff_fingerprint(before, after), ["sneaky.py"])

    def test_untouched_workspace_reports_nothing(self) -> None:
        before = team.workspace_fingerprint(self.ws)
        self.assertEqual(team._diff_fingerprint(before, team.workspace_fingerprint(self.ws)), [])

    def test_cmd_review_records_who_ran_it_and_whether_it_wrote(self) -> None:
        """任务卡要能反向追溯：这一轮是谁复核的、沙箱是什么、动没动手。"""
        src = inspect.getsource(team.cmd_review)
        self.assertIn('"sandbox": sandbox', src)
        self.assertIn('"wrote": touched', src)
        self.assertIn("review_wrote", src, "动过手要在任务卡上留痕")


class ReviewerConfigCannotWidenTests(unittest.TestCase):
    """配置文件把 reviewer 写成可写，也不许生效。"""

    def test_config_with_writable_sandbox_is_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "team.json"
            cfg.write_text(json.dumps({"reviewer": {"sandbox": "workspace-write"}},
                                      ensure_ascii=False), encoding="utf-8")
            saved = team.CONFIG_PATH
            team.CONFIG_PATH = cfg
            try:
                loaded = team.load_reviewer_config()
            finally:
                team.CONFIG_PATH = saved
            self.assertEqual(loaded["sandbox"], "workspace-write",
                             "配置读进来是什么就是什么（不在这里偷偷改）")
            # 真正的拦截点在复核入口：读进来是可写，跑的时候一定被降级
            self.assertEqual(team._review_sandbox(loaded["sandbox"]), "read-only")


if __name__ == "__main__":
    unittest.main()