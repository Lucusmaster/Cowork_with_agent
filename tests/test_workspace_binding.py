"""工作区绑定回归测试。

守的是这次踩过的坑：任务卡绑错工作区，产物写到协作账本仓库里，
跑完才发现「代码进了账本、项目里啥也没有」。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "路径识别用例基于 Windows 盘符")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 包在仓库根

from agent_team.winproc import quiet_flags  # noqa: E402
sys.path.insert(0, str(ROOT))  # 包在仓库根

from agent_team import worker  # noqa: E402


class DetectWorkspaceTests(unittest.TestCase):
    """从一句自然语言需求里认出工作区。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        self.bare = self.tmp / "no-repo"
        self.bare.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @WINDOWS_ONLY
    def test_repo_root_directory(self) -> None:
        ws, why = worker.detect_workspace(f"改 {self.repo} 里的东西")
        self.assertEqual(ws, self.repo.resolve())
        self.assertEqual(why, "")

    @WINDOWS_ONLY
    def test_subdirectory_binds_to_repo_root(self) -> None:
        # 绑子目录会让 git diff 和验收命令错位，必须上浮到仓库根
        ws, _ = worker.detect_workspace(f"给 {self.repo / 'src'} 加个函数")
        self.assertEqual(ws, self.repo.resolve())

    @WINDOWS_ONLY
    def test_file_path_binds_to_repo_root(self) -> None:
        ws, _ = worker.detect_workspace(f"给 {self.repo / 'src' / 'main.py'} 加一行")
        self.assertEqual(ws, self.repo.resolve())

    @WINDOWS_ONLY
    def test_directory_without_git_is_itself(self) -> None:
        ws, _ = worker.detect_workspace(f"给 {self.bare} 加个脚本")
        self.assertEqual(ws, self.bare.resolve())

    @WINDOWS_ONLY
    def test_missing_path_is_rejected_not_silently_ledger(self) -> None:
        ws, why = worker.detect_workspace(f"给 {self.tmp / 'nope' / 'x.py'} 加东西")
        self.assertIsNone(ws, "路径不存在时必须拒绝，不能默默绑到协作仓库")
        self.assertIn("不存在", why)

    @WINDOWS_ONLY
    def test_no_path_is_rejected(self) -> None:
        # 这条是原始 bug 的入口：以前会默默绑到协作仓库
        ws, why = worker.detect_workspace("优化示例项目的导出功能")
        self.assertIsNone(ws)
        self.assertIn("没有指定", why)

    @WINDOWS_ONLY
    def test_ledger_hint_stays_in_ledger(self) -> None:
        ws, _ = worker.detect_workspace("给 tools/wordstat.py 加 --exclude")
        self.assertEqual(ws, ROOT)

    @WINDOWS_ONLY
    def test_two_directories_is_ambiguous(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        ws, why = worker.detect_workspace(f"把 {self.repo} 和 {other} 都改一下")
        self.assertIsNone(ws)
        self.assertIn("多个目录", why)

    @WINDOWS_ONLY
    def test_quoted_path(self) -> None:
        ws, _ = worker.detect_workspace(f'给 "{self.repo}" 加个 README')
        self.assertEqual(ws, self.repo.resolve())

    @WINDOWS_ONLY
    def test_two_files_same_repo_is_not_ambiguous(self) -> None:
        (self.repo / "src" / "util.py").write_text("", encoding="utf-8")
        text = f"改 {self.repo / 'src' / 'main.py'} 和 {self.repo / 'src' / 'util.py'}"
        ws, why = worker.detect_workspace(text)
        self.assertEqual(ws, self.repo.resolve(), why)


class TaskCardWorkspaceTests(unittest.TestCase):
    """team.py new --workspace 的入参必须校验，不能记下一个不存在的路径。"""

    def test_missing_workspace_is_rejected(self) -> None:
        missing = Path(tempfile.gettempdir()) / "dsh-not-exist-xyz"
        proc = subprocess.run(
            [sys.executable, "-m", "agent_team.team", "new",
             "--title", "绑定测试", "--goal", "确保不会建出绑错的卡",
             "--acceptance", "不会创建任务卡",
             "--verify", "python -m unittest discover -s tests -t . -v",
             "--workspace", str(missing)],
            capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
            creationflags=quiet_flags(),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不存在", proc.stdout + proc.stderr)

    def test_file_as_workspace_is_rejected(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_team.team", "new",
             "--title", "绑定测试", "--goal", "目录不能是文件",
             "--acceptance", "不会创建任务卡",
             "--verify", "python -m unittest discover -s tests -t . -v",
             "--workspace", str(ROOT / "README.md")],
            capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
            creationflags=quiet_flags(),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("必须是目录", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
