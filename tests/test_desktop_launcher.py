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

    def test_app_mode_opens_a_window_without_address_bar(self) -> None:
        """`--app`：交给 Edge/Chrome 的 `--app=`，不是再开一个浏览器标签页。"""
        with mock.patch.object(launcher, "page_ready", return_value=True), \
             mock.patch.object(launcher, "one_time_url", return_value="http://x/?k=1"), \
             mock.patch.object(launcher, "app_browser", return_value=r"C:\Edge\msedge.exe"), \
             mock.patch("agent_team.launcher.subprocess.Popen") as popen, \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            code = launcher.main(["--app"])
        self.assertEqual(code, 0)
        opened.assert_not_called()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], r"C:\Edge\msedge.exe")
        self.assertEqual(argv[1], "--app=http://x/?k=1")

    def test_app_mode_falls_back_to_default_browser(self) -> None:
        """没有 Edge/Chrome 也不能白开一次：退回默认浏览器，别让用户对着空气。"""
        with mock.patch.object(launcher, "page_ready", return_value=True), \
             mock.patch.object(launcher, "one_time_url", return_value="http://x/?k=1"), \
             mock.patch.object(launcher, "app_browser", return_value=""), \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            code = launcher.main(["--app"])
        self.assertEqual(code, 0)
        opened.assert_called_once_with("http://x/?k=1")

    def test_no_browser_wins_over_app(self) -> None:
        with mock.patch.object(launcher, "page_ready", return_value=True), \
             mock.patch.object(launcher, "one_time_url", return_value="http://x/?k=1"), \
             mock.patch.object(launcher, "app_browser", return_value=r"C:\Edge\msedge.exe"), \
             mock.patch("agent_team.launcher.subprocess.Popen") as popen, \
             mock.patch.object(launcher.webbrowser, "open") as opened:
            self.assertEqual(launcher.main(["--app", "--no-browser"]), 0)
        popen.assert_not_called()
        opened.assert_not_called()

    def test_app_browser_is_a_real_file_when_found(self) -> None:
        found = launcher.app_browser()
        if found:
            self.assertTrue(Path(found).is_file(), f"找到的浏览器不存在：{found}")


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

    def test_ps1_supports_icon_override(self) -> None:
        """图标：传了就用传的，没传退回系统图标——别让快捷方式变白方块。"""
        self.assertIn("[string]$Icon", self.text)
        self.assertIn("$Link.IconLocation = (Resolve-Path $Icon).Path", self.text)
        self.assertIn("shell32.dll,13", self.text, "没有图标时必须还有系统图标兜底")

    def test_ps1_supports_app_mode(self) -> None:
        """App 版：指向 pythonw（不弹黑框）+ `-m agent_team.launcher --app`，另起一个 .lnk。"""
        self.assertIn("[switch]$App", self.text)
        self.assertIn("[string]$PythonW", self.text)
        self.assertIn("agent-team-app.lnk", self.text)
        self.assertIn("-m agent_team.launcher --app", self.text)

    @unittest.skipIf(os.name != "nt", "快捷方式只有 Windows 有")
    def test_creates_app_shortcut_targeting_pythonw(self) -> None:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("这台机器没有 pythonw.exe")
        with tempfile.TemporaryDirectory() as desktop:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
                 "-ProjectPath", str(REPO), "-Desktop", desktop, "-App",
                 "-PythonW", str(pythonw), "-Force"],
                capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lnk = Path(desktop) / "agent-team-app.lnk"
            self.assertTrue(lnk.is_file(), f"快捷方式没生成：{lnk}")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
                 f"$s.TargetPath; $s.Arguments"],
                capture_output=True, encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW).stdout
            self.assertIn("pythonw.exe", out)
            self.assertIn("--app", out)

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


class IconAssetTests(unittest.TestCase):
    """应用图标：脚本能重跑、ico 尺寸齐全、png 是给人眼看的预览。"""

    SIZES = (16, 24, 32, 48, 64, 128, 256)

    def setUp(self) -> None:
        # 生成到临时目录：测试不许改写仓库里已提交的二进制。不同机器上 Pillow 版本产出的
        # 字节不一定一样，写回仓库就会让 assets/ 显示"被改过"（协作台在另一台机器上就踩到了）。
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ico(self) -> Path:
        return self.out / "agent-team.ico"

    def png(self) -> Path:
        return self.out / "agent-team.png"

    def _regen(self) -> None:
        proc = subprocess.run([sys.executable, str(REPO / "bin" / "make_icon.py"),
                               "--out", str(self.out)],
                              capture_output=True, encoding="utf-8", errors="replace",
                              cwd=str(REPO), creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_repo_ships_the_icons(self) -> None:
        """仓库里带上成品：装快捷方式的人不该被要求先装 Pillow 再生成。"""
        self.assertTrue((REPO / "assets" / "agent-team.ico").is_file())
        self.assertTrue((REPO / "assets" / "agent-team.png").is_file())

    def test_generator_is_not_gitignored(self) -> None:
        """0.9.21 的坑：脚本放在被 `.gitignore` 忽略的 `tools/` 里，说"随仓库发布"其实没进仓库。

        查的是"没被忽略"而不是"已跟踪"——新文件还没 `git add` 时也该是这个结论。
        """
        script = REPO / "bin" / "make_icon.py"
        self.assertTrue(script.is_file(), "生成脚本不在 bin/ 下")
        try:
            proc = subprocess.run(["git", "check-ignore", "-v", str(script.relative_to(REPO))],
                                  capture_output=True, encoding="utf-8", errors="replace",
                                  cwd=str(REPO), creationflags=NO_WINDOW)
        except OSError:
            self.skipTest("没有 git")
        if proc.returncode not in (0, 1):
            self.skipTest("这里不是 git 工作区")
        self.assertEqual(proc.returncode, 1,
                         f"生成脚本被忽略了（{proc.stdout.strip()}）：换到没被忽略的目录")

    def test_script_regenerates_both_files(self) -> None:
        self._regen()
        self.assertTrue(self.ico().is_file())
        self.assertTrue(self.png().is_file())

    def test_ico_carries_every_size(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没装 Pillow")
        self._regen()
        with Image.open(self.ico()) as im:
            sizes = {tuple(size) for size in im.info.get("sizes", [])}
        for size in self.SIZES:
            self.assertIn((size, size), sizes, f"ico 里缺 {size}x{size}")

    def test_png_is_a_256_preview(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没装 Pillow")
        self._regen()
        with Image.open(self.png()) as im:
            self.assertEqual(im.size, (256, 256))
            self.assertEqual(im.mode, "RGBA")

    def test_tiny_size_drops_the_core_and_links(self) -> None:
        """16 这一档只留三个点：白点和连线在小网格上只会糊成一团。"""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("没装 Pillow")
        self._regen()
        with Image.open(self.ico()) as im:
            im.size = (16, 16)
            pixels = list(im.convert("RGBA").getdata())
        self.assertFalse([p for p in pixels if p[:3] == (255, 255, 255)],
                         "16px 上不该再有白点")
        self.assertTrue([p for p in pixels if p[:3] == (0x3F, 0xD3, 0xA6)],
                        "16px 上要能看到节点")

    def test_small_frames_are_bmp_not_png(self) -> None:
        """16..128 必须是 BMP 帧。

        Pillow 的 ICO 写入器会把每一帧都压成 PNG，Windows 在 16..128 上读不出来，
        桌面快捷方式就显示成一个白板（"像无法识别的文件"）。256 用 PNG 才是惯例。
        """
        import struct

        self._regen()
        data = self.ico().read_bytes()
        _res, _typ, count = struct.unpack_from("<HHH", data, 0)
        self.assertEqual(count, 7)
        offset = 6
        for _ in range(count):
            width, _height, _colors, _reserved, _planes, _bpp, size, start = struct.unpack_from(
                "<BBBBHHII", data, offset)
            offset += 16
            is_png = data[start:start + 8] == b"\x89PNG\r\n\x1a\n"
            if width in (0, 256):
                self.assertTrue(is_png, "256 这一档应该用 PNG")
            else:
                self.assertFalse(is_png, f"{width}px 这一档必须用 BMP，不能是 PNG")
                header_size = struct.unpack_from("<I", data, start)[0]
                self.assertEqual(header_size, 40, f"{width}px 帧头不是 BITMAPINFOHEADER")

    @unittest.skipIf(os.name != "nt", "只有 Windows 有 shell 图标提取")
    def test_windows_shell_can_extract_the_icon(self) -> None:
        """直接问 Windows 要图标：这正是 Explorer 画桌面图标的那条路。

        旧版把所有帧压成 PNG 时，这个调用返回 S_FALSE 且拿不到句柄——图标就成了白板。
        """
        import ctypes
        from ctypes import wintypes

        self._regen()
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        extract = shell32.SHDefExtractIconW
        extract.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_uint,
                            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
                            ctypes.c_uint]
        extract.restype = ctypes.c_long
        for large, small in ((48, 16), (32, 16)):
            big, tiny = wintypes.HICON(), wintypes.HICON()
            code = extract(str(self.ico()), 0, 0,
                           ctypes.byref(big), ctypes.byref(tiny), large | (small << 16))
            try:
                self.assertEqual(code, 0, f"{large}/{small} 取图标失败：0x{code & 0xFFFFFFFF:08x}")
                self.assertTrue(big.value, f"{large}/{small} 没拿到大图标句柄")
                self.assertTrue(tiny.value, f"{large}/{small} 没拿到小图标句柄")
            finally:
                for handle in (big, tiny):
                    if handle.value:
                        ctypes.windll.user32.DestroyIcon(handle)

    @unittest.skipIf(os.name != "nt", "快捷方式只有 Windows 有")
    def test_shortcut_can_use_the_new_icon(self) -> None:
        ico = REPO / "assets" / "agent-team.ico"
        if not ico.is_file():
            self.skipTest("图标还没生成")
        with tempfile.TemporaryDirectory() as desktop:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1),
                 "-ProjectPath", str(REPO), "-Desktop", desktop, "-Icon", str(ico), "-Force"],
                capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lnk = Path(desktop) / "agent-team.lnk"
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); "
                 f"$s.IconLocation"],
                capture_output=True, encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW).stdout
            self.assertIn("agent-team.ico", out)


class InstallDesktopTests(unittest.TestCase):
    def test_cli_exposes_install_desktop(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "agent_team.cli", "install-desktop", "--help"],
                              capture_output=True, encoding="utf-8", errors="replace",
                              creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--desktop", proc.stdout)
        self.assertIn("--app", proc.stdout)

    def test_refuses_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "agent_team.cli", "install-desktop", "--workspace", tmp],
                capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("git", proc.stdout)


if __name__ == "__main__":
    unittest.main()