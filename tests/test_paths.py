"""账本根目录的选择：仓库内、pip 安装后、环境变量覆盖。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_team import paths


class WorkbenchRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.checkout = self.base / "repo"
        (self.checkout / "agent_team").mkdir(parents=True)
        (self.checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        self.site = self.base / "site-packages" / "agent_team"
        self.site.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_checkout_uses_repo_root(self) -> None:
        pkg = self.checkout / "agent_team" / "__init__.py"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(paths.ENV_HOME, None)
            self.assertEqual(paths.workbench_root(pkg_file=pkg), self.checkout.resolve())

    def test_installed_does_not_use_site_packages(self) -> None:
        pkg = self.site / "__init__.py"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(paths.ENV_HOME, None)
            got = paths.workbench_root(pkg_file=pkg, home=self.base / "userhome")
        self.assertNotEqual(got, self.site)
        self.assertEqual(got, (self.base / "userhome" / ".agent-team").resolve())

    def test_env_override_wins(self) -> None:
        with patch.dict(os.environ, {paths.ENV_HOME: str(self.base / "data")}):
            got = paths.workbench_root(pkg_file=self.checkout / "agent_team" / "__init__.py")
        self.assertEqual(got, (self.base / "data").resolve())

    def test_ensure_root_makes_dirs(self) -> None:
        target = self.base / "data2"
        got = paths.ensure_root(target)
        self.assertTrue((got / "tasks").is_dir())
        self.assertTrue((got / "room").is_dir())


if __name__ == "__main__":
    unittest.main()
