"""桌面启动器：逻辑、脚本契约、快捷方式。

背景：外部方案交来一版启动器，看着完整，但三处假设跟实际 CLI/接口不符，
双击必失败（`cli start --workspace` 语法错、探活打的是不存在的 `/api/status`、
打开的裸地址在 0.9.12 之后只会显示解锁页）。它那 6 个"测试"是 pytest 风格，
在 `unittest discover` 里一条都不会被收集——"全绿"是假的。

这组测试钉住"双击后真的能用"的三件事：
1. launcher 的判断逻辑（已在跑就只开页面、没在跑才启动、起不来要报错）；
2. 脚本内容契约（.bat 只做转交、.ps1 直接指向 .bat、都不放非 ASCII）；
3. 快捷方式确实指向启动脚本。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import launcher

REPO = Path(__file__).resolve().parent.parent
BAT = REPO / "bin" / "agent-team-launcher.bat"
PS1 = REPO / "bin" / "install-shortcut.ps1"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LauncherLogicTests(unittest.TestCase):
    def test_url_uses_one_time_code(self) -> None:
        """必须是 ?k=：长期令牌进 URL 已经在 0.9.12 被禁掉了。"""
        with tempfile.TemporaryDirectory() as tmp:
            code_file = Path(tmp) / "web-code"
            code_file.write_text("abc123def456abc123def456abc12345\n", encoding="utf-8")
            with mock.patch.object(launcher, "CODE_FILE", code_file):
                url = launcher.one_time_url(8899)
        self.assertEqual(url, "http://127.0.0.1:8899/?k=abc123def456abc123def456abc12345")
        self.assertNotIn("token=", url)

    def test_url_without_code_falls_back_to_bare_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(launcher, "CODE_FILE", Path(tmp) / "nope"):
                self.assertEqual(launcher.one_time_url(8899), "http://127.0.0.1:8899/")

    def test_already_running_does_not_restart(self) -> None:
        """已经在跑时重启会打断正在执行的任务——只开页面。"""
        with mock.patch.object(launcher, "page_ready", return_value=True), \
             mock.patch.object(launcher, "start_server") as start, \
             mock.patch.object(launcher, "one_time_url", return_value="http://x/?k=1"), \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            code = launcher.main(["--port", "8899"])
        self.assertEqual(code, 0)
        start.assert_not_called()
        opened.assert_called_once_with("http://x/?k=1")

    def test_not_running_starts_then_opens(self) -> None:
        with mock.patch.object(launcher, "page_ready", side_effect=[False, True]), \
             mock.patch.object(launcher, "port_open", return_value=False), \
             mock.patch.object(launcher, "start_server", return_value=0) as start, \
             mock.patch.object(launcher, "wait_ready", return_value=True), \
             mock.patch.object(launcher, "one_time_url", return_value="http://x/?k=1"), \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            code = launcher.main(["--port", "8899"])
        self.assertEqual(code, 0)
        start.assert_called_once_with(8899)
        opened.assert_called_once_with("http://x/?k=1")

    def test_start_failure_reports_error(self) -> None:
        with mock.patch.object(launcher, "page_ready", return_value=False), \
             mock.patch.object(launcher, "port_open", return_value=False), \
             mock.patch.object(launcher, "start_server", return_value=2), \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            code = launcher.main(["--port", "8899"])
        self.assertEqual(code, 1)
        opened.assert_not_called()

    def test_timeout_does_not_open_browser(self) -> None:
        with mock.patch.object(launcher, "page_ready", return_value=False), \
             mock.patch.object(launcher, "port_open", return_value=False), \
             mock.patch.object(launcher, "start_server", return_value=0), \
             mock.patch.object(launcher, "wait_ready", return_value=False), \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            code = launcher.main(["--port", "8899"])
        self.assertEqual(code, 1)
        opened.assert_not_called()

    def test_port_taken_by_something_else_is_not_restarted(self) -> None:
        """端口被别的程序占着：不要盲目起服务（会撞端口），说清楚就退出。"""
        with mock.patch.object(launcher, "page_ready", return_value=False), \
             mock.patch.object(launcher, "port_open", return_value=True), \
             mock.patch.object(launcher, "start_server") as start:
            code = launcher.main(["--port", "8899"])
        self.assertEqual(code, 1)
        start.assert_not_called()

    def test_no_browser_flag(self) -> None:
        with mock.patch.object(launcher, "page_ready", return_value=True), \
             mock.patch.object(launcher, "one_time_url", return_value="http://x/?k=1"), \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            self.assertEqual(launcher.main(["--no-browser"]), 0)
        opened.assert_not_called()

    def test_probe_uses_root_page_not_invented_endpoint(self) -> None:
        """探活打的是 `GET /`（无需令牌），不是外部方案臆造的 /api/status。"""
        src = (REPO / "agent_team" / "launcher.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/status", src, "那个接口不存在，探活永远不会成功")
        self.assertIn('f"http://{host}:{port}/"', src)

    def test_page_ready_false_on_closed_port(self) -> None:
        self.assertFalse(launcher.page_ready(1, timeout=0.3))


class BatContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = BAT.read_text(encoding="utf-8")

    def test_launcher_exists_and_is_ascii_only(self) -> None:
        self.assertTrue(BAT.is_file())
        self.assertTrue(self.text.isascii(),
                        "cmd.exe 按控制台代码页读 .bat，非 ASCII 会在某些机器上写坏命令")

    def test_bat_hands_off_to_python(self) -> None:
        self.assertIn("agent_team.launcher", self.text)
        self.assertIn('cd /d "%~dp0.."', self.text, "双击时工作目录是 bin/，必须切回仓库根")

    def test_bat_does_not_pass_unsupported_flags(self) -> None:
        """`cli start` 只认 --port/--restart；--workspace 是外部方案臆造的参数。"""
        self.assertNotIn("cli start", self.text)
        self.assertNotIn("--workspace", self.text)

    def test_bat_does_not_use_the_nonexistent_status_endpoint(self) -> None:
        self.assertNotIn("/api/status", self.text)

    def test_bat_pauses_in_failure_branch(self) -> None:
        self.assertIn("pause", self.text)
        self.assertLess(self.text.index("pause"), self.text.index("exit /b 1"),
                        "失败分支里要先 pause，否则窗口一闪而过看不到原因")


class Ps1ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = PS1.read_text(encoding="utf-8")

    def test_ps1_is_ascii_only(self) -> None:
        """PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，中文直接导致语法错误。"""
        self.assertTrue(self.text.isascii(),
                        "5.1 上非 ASCII .ps1 会报 'The string is missing the terminator'")

    def test_ps1_targets_the_bat_directly(self) -> None:
        self.assertIn("agent-team-launcher.bat", self.text)
        self.assertIn("$Link.TargetPath = $Launcher", self.text,
                      "TargetPath 要直接指向 .bat，别绕 cmd.exe /c 的引号")
        self.assertIsNone(re.search(r"TargetPath\s*=\s*['\"]cmd\.exe", self.text),
                          "绕 cmd.exe /c 的引号是 Windows 上最常见的坑")
        self.assertIn("$PSScriptRoot", self.text, "要能自己算出仓库根，而不是依赖调用者的 cwd")

    def test_ps1_supports_test_desktop_and_force(self) -> None:
        self.assertIn("[string]$Desktop", self.text)
        self.assertIn("[switch]$Force", self.text)

    @unittest.skipIf(sys.platform != "win32", "PowerShell 只在 Windows 上验")
    def test_ps1_parses_on_powershell(self) -> None:
        """真的让 PowerShell 解析一遍：这是发现编码/引号问题最省事的方式。"""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$null = [scriptblock]::Create([IO.File]::ReadAllText('{PS1}')); 'OK'"],
            capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)

    @unittest.skipIf(os.name != "nt", "快捷方式只有 Windows 有")
    def test_creates_a_shortcut_pointing_at_the_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as desktop:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
                 "-ProjectPath", str(REPO), "-Desktop", desktop, "-Force"],
                capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lnk = Path(desktop) / "agent-team.lnk"
            self.assertTrue(lnk.is_file(), f"快捷方式没生成：{lnk}")
            target = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); $s.TargetPath"],
                capture_output=True, encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW).stdout
            self.assertIn("agent-team-launcher.bat", target)

    @unittest.skipIf(os.name != "nt", "快捷方式只有 Windows 有")
    def test_existing_shortcut_is_not_overwritten_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as desktop:
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
                    "-ProjectPath", str(REPO), "-Desktop", desktop]
            first = subprocess.run([*args, "-Force"], capture_output=True, creationflags=NO_WINDOW)
            self.assertEqual(first.returncode, 0)
            again = subprocess.run([*args], capture_output=True, encoding="utf-8",
                                   errors="replace", creationflags=NO_WINDOW)
            self.assertEqual(again.returncode, 2, "已存在时应返回 2，提示加 -Force")


class InstallDesktopTests(unittest.TestCase):
    def test_cli_exposes_install_desktop(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "agent_team.cli", "install-desktop", "--help"],
                              capture_output=True, encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--desktop", proc.stdout)

    def test_refuses_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "agent_team.cli", "install-desktop", "--workspace", tmp],
                capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("git", proc.stdout)


if __name__ == "__main__":
    unittest.main()