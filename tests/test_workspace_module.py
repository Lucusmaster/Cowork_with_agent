"""工作区模块测试（契约第 3 节的 workspace.py）。

守的规则：必须存在、必须是目录、必须能上浮到最近的 .git 根、
不许把协作台自己当工作区、工作区内的相对路径不许越界、任务 ID 只认 T-######-######。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 包在仓库根
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent_team import workspace  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_strips_spaces_and_quotes(self) -> None:
        self.assertEqual(workspace.normalize('  "E:\\proj\\"  '), "E:\\proj")
        self.assertEqual(workspace.normalize("“E:\\proj”"), "E:\\proj")

    def test_strips_trailing_slashes_but_keeps_drive_root(self) -> None:
        self.assertEqual(workspace.normalize("E:\\proj\\"), "E:\\proj")
        self.assertEqual(workspace.normalize("E:\\proj/"), "E:\\proj")
        self.assertEqual(workspace.normalize("E:\\"), "E:\\")

    def test_strips_file_uri_prefix(self) -> None:
        self.assertEqual(workspace.normalize("file:///E:/proj/"), "E:/proj")
        self.assertEqual(workspace.normalize("file:E:\\proj"), "E:\\proj")

    def test_blank_stays_blank(self) -> None:
        self.assertEqual(workspace.normalize("   "), "")
        self.assertEqual(workspace.normalize(None), "")


class ValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "proj"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        self.bare = self.tmp / "no-repo"
        self.bare.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_repo_root_ok(self) -> None:
        found, why = workspace.validate(str(self.repo))
        self.assertEqual(found, self.repo.resolve())
        self.assertEqual(why, "")

    def test_subdirectory_uplifts_to_git_root(self) -> None:
        found, why = workspace.validate(str(self.repo / "src"))
        self.assertEqual(found, self.repo.resolve(), why)

    def test_quoted_path_ok(self) -> None:
        found, _ = workspace.validate(f'"{self.repo}"')
        self.assertEqual(found, self.repo.resolve())

    def test_missing_rejected(self) -> None:
        found, why = workspace.validate(str(self.tmp / "nope"))
        self.assertIsNone(found)
        self.assertIn("不存在", why)

    def test_file_rejected(self) -> None:
        target = self.tmp / "a.py"
        target.write_text("", encoding="utf-8")
        found, why = workspace.validate(str(target))
        self.assertIsNone(found)
        self.assertIn("必须是目录", why)

    def test_non_git_rejected(self) -> None:
        found, why = workspace.validate(str(self.bare))
        self.assertIsNone(found)
        self.assertIn("git", why)

    def test_ledger_itself_rejected(self) -> None:
        found, why = workspace.validate(str(ROOT))
        self.assertIsNone(found)
        self.assertIn("协作台", why)

    def test_relative_path_rejected(self) -> None:
        found, why = workspace.validate("proj")
        self.assertIsNone(found)
        self.assertIn("绝对路径", why)

    def test_blank_rejected(self) -> None:
        found, why = workspace.validate("   ")
        self.assertIsNone(found)
        self.assertIn("没有指定", why)


class InsideLedgerTests(unittest.TestCase):
    def test_ledger_root_and_child_are_inside(self) -> None:
        self.assertTrue(workspace.is_inside_ledger(ROOT))
        self.assertTrue(workspace.is_inside_ledger(ROOT / "agent_team"))

    def test_other_repo_is_outside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "proj"
            other.mkdir()
            self.assertFalse(workspace.is_inside_ledger(other))


class TaskIdTests(unittest.TestCase):
    def test_valid_ids(self) -> None:
        self.assertTrue(workspace.is_valid_task_id("T-261031-120000"))
        self.assertTrue(workspace.is_valid_task_id("T-000000-000000"))

    def test_invalid_ids(self) -> None:
        for bad in ("T-1", "T-26103-120000", "T-261031-12000", "T-abcdef-120000",
                    "261031-120000", "T-261031-120000X", ""):
            self.assertFalse(workspace.is_valid_task_id(bad), bad)

    def test_validate_task_id_message(self) -> None:
        ok, why = workspace.validate_task_id("T-261031-120000")
        self.assertTrue(ok)
        self.assertEqual(why, "")
        ok, why = workspace.validate_task_id("T-1")
        self.assertFalse(ok)
        self.assertIn("任务 ID 不合法", why)


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        (self.root / ".git").mkdir()
        self.outside = self.root.parent / "outside"
        self.outside.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inside_path_ok(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, "src/login.py")
        self.assertTrue(ok, why)

    def test_dot_slash_prefix_ok(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, "./src/login.py")
        self.assertTrue(ok, why)

    def test_parent_escape_blocked(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, "../outside/evil.py")
        self.assertFalse(ok)
        self.assertIn("越界", why)

    def test_deep_escape_blocked(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, "src/../../evil.py")
        self.assertFalse(ok)
        self.assertIn("越界", why)

    def test_absolute_path_blocked(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, str(self.outside / "evil.py"))
        self.assertFalse(ok)
        self.assertIn("相对路径", why)

    def test_empty_path_blocked(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, "   ")
        self.assertFalse(ok)
        self.assertIn("不能为空", why)

    def test_valid_task_path_ok(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, "tasks/T-261031-120000/exec-01.last.md")
        self.assertTrue(ok, why)

    def test_invalid_task_id_blocked(self) -> None:
        ok, why = workspace.ensure_workspace_scope(self.root, "tasks/T-123/exec-01.last.md")
        self.assertFalse(ok)
        self.assertIn("任务 ID 不合法", why)

    def test_missing_workspace_blocked(self) -> None:
        ok, why = workspace.ensure_workspace_scope(None, "src/login.py")
        self.assertFalse(ok)
        self.assertIn("工作区", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)