"""守住"后台干活不许弹黑框"这条：所有子进程调用都必须带隐藏窗口标志。

Windows 上 subprocess 默认会新建一个控制台窗口，引擎每次跑 codex / git / python
都会闪一下黑框。谁以后新加子进程调用却忘了 creationflags，这里就会失败。
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPAWN = re.compile(r"subprocess\.(run|Popen|call)\(")
LEGACY = re.compile(r"os\.(system|popen|spawn\w*)\(")


def call_sites(text: str):
    for match in SPAWN.finditer(text):
        start = match.start()
        depth, index = 0, start + len(match.group(0)) - 1
        while index < len(text):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        yield text[:start].count("\n") + 1, text[start:index + 1]


class QuietSubprocessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "CREATE_NO_WINDOW 只在 Windows 有意义")
    def test_every_spawn_hides_console_window(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "agent_team").glob("*.py")):
            for line_no, call in call_sites(path.read_text(encoding="utf-8")):
                if "creationflags" not in call:
                    offenders.append(f"{path.name}:{line_no}")
        self.assertEqual(offenders, [], f"这些子进程调用没加 creationflags，会弹黑框：{offenders}")

    def test_no_legacy_os_system_calls(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "agent_team").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in LEGACY.finditer(text):
                offenders.append(f"{path.name}:{text[:match.start()].count(chr(10)) + 1}")
        self.assertEqual(offenders, [], f"os.system/os.popen 无法隐藏窗口，请改成 subprocess：{offenders}")

    def test_winproc_helper_exists(self) -> None:
        module = ROOT / "agent_team" / "winproc.py"
        self.assertTrue(module.is_file())
        self.assertIn("quiet_flags", module.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()