"""使用说明（skill）本身也要被测试——不然文档会悄悄漂移成错的。

这套测试的来源很具体：旧版 skill 里写着 `python bin/room.py post ...`，
而 `bin/room.py` 早就不存在了。照着做的 agent 第一条命令就失败。
所以这里做两件事：

1. **文档漂移守卫**：SKILL.md 里提到的每条命令/模块/脚本，都拿去和真实的 CLI、包、文件比对；
2. **安装器行为**：装到 DSH / Codex / Claude Code 三个位置，幂等、先备份再覆盖、dry-run 不动盘。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_team import skills_install

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / skills_install.SKILL_NAME
SKILL = SKILL_DIR / "SKILL.md"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def cli_help() -> str:
    proc = subprocess.run([sys.executable, "-m", "agent_team.cli", "--help"],
                          capture_output=True, encoding="utf-8", errors="replace",
                          cwd=str(REPO), creationflags=NO_WINDOW)
    return proc.stdout


def known_subcommands() -> set[str]:
    text = cli_help()
    hit = re.search(r"\{([a-z0-9,\-]+)\}", text)
    return set(hit.group(1).split(",")) if hit else set()


class SkillContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def test_skill_file_exists_with_frontmatter(self) -> None:
        self.assertTrue(SKILL.is_file(), f"正本不在：{SKILL}")
        self.assertTrue(self.text.startswith("---\n"), "frontmatter 必须在第一行")
        head = self.text.split("---")[1]
        name = re.search(r"^name:\s*(\S+)", head, re.M)
        self.assertIsNotNone(name, "frontmatter 缺 name")
        self.assertEqual(name.group(1), skills_install.SKILL_NAME,
                         "name 必须和目录名一致，否则 agent 加载时会认不出来")
        desc = re.search(r"^description:\s*(.+)$", head, re.M)
        self.assertIsNotNone(desc, "frontmatter 缺 description（决定 agent 什么时候用它）")
        self.assertGreater(len(desc.group(1)), 80, "description 要能让人/模型判断何时该用")

    def test_every_cli_command_mentioned_really_exists(self) -> None:
        """文档漂移守卫：写了不存在的子命令，照着做的 agent 会在第一步就撞墙。"""
        subs = known_subcommands()
        self.assertTrue(subs, "没解析出 CLI 子命令")
        mentioned = set(re.findall(r"agent_team\.cli\s+([a-z][a-z-]*)", self.text))
        mentioned |= set(re.findall(r"^\s*agent-team\s+([a-z][a-z-]*)", self.text, re.M))
        self.assertTrue(mentioned, "skill 里应该给出真实命令")
        unknown = {c for c in mentioned if c not in subs and c not in {"model", "mcp"}}
        self.assertEqual(unknown, set(),
                         f"skill 提到了 CLI 里没有的子命令：{unknown}（现有：{sorted(subs)}）")

    def test_every_module_mentioned_is_importable(self) -> None:
        for mod in set(re.findall(r"python -m agent_team\.([a-z_]+)", self.text)):
            self.assertTrue((REPO / "agent_team" / f"{mod}.py").is_file(),
                            f"skill 提到 `python -m agent_team.{mod}`，但模块不存在")

    def test_every_bin_script_mentioned_exists(self) -> None:
        for name in set(re.findall(r"bin/([A-Za-z0-9_\-]+\.(?:py|ps1|bat|sh))", self.text)):
            self.assertTrue((REPO / "bin" / name).is_file(), f"skill 提到 bin/{name}，但它不存在")

    def test_does_not_mention_removed_helpers(self) -> None:
        """这些是历史上删掉的东西，写进去就是错的。"""
        self.assertNotIn("bin/room.py", self.text, "bin/room.py 早已删除，用 agent-team say/tail")
        self.assertNotIn("bin/start_all.py --status", self.text, "用 agent-team status")

    def test_tells_you_to_use_the_one_time_link(self) -> None:
        """0.9.12 之后裸地址只会显示解锁页，skill 必须说清楚。"""
        self.assertIn("?k=", self.text)
        self.assertIn("web-token", self.text)

    def test_warns_about_auto_stop_traps(self) -> None:
        """90 秒静默收工 + 后台标签页节流，是真实踩过的坑，必须写在里面。"""
        self.assertIn("--no-autostop", self.text)
        self.assertIn("节流", self.text)

    def test_states_that_task_card_lacks_requester(self) -> None:
        """MCP 身份只记在 meta.client——别让 agent 以为卡里有。"""
        self.assertIn("meta.client", self.text)


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        for name in (".dsh", ".codex", ".claude"):
            (self.home / name).mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_detects_existing_clients(self) -> None:
        self.assertEqual(set(skills_install.detect(self.home)), {"dsh", "codex", "claude"})

    def test_installs_to_all_three_layouts(self) -> None:
        results = skills_install.install_all(home=self.home)
        self.assertTrue(all(r["ok"] for r in results), results)
        for target, base in (("dsh", ".dsh"), ("codex", ".codex"), ("claude", ".claude")):
            dest = self.home / base / "skills" / skills_install.SKILL_NAME / "SKILL.md"
            self.assertTrue(dest.is_file(), f"{target} 没装上：{dest}")
            self.assertEqual(dest.read_text(encoding="utf-8"), SKILL.read_text(encoding="utf-8"))

    def test_second_install_is_a_noop(self) -> None:
        skills_install.install_all(home=self.home)
        again = skills_install.install_all(home=self.home)
        self.assertTrue(all(r["action"] == "same" for r in again), again)

    def test_edited_copy_is_backed_up_not_lost(self) -> None:
        """用户可能手改过：默认先备份再覆盖，不许直接冲掉。"""
        dest = self.home / ".codex" / "skills" / skills_install.SKILL_NAME / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("我的手改版\n", encoding="utf-8")
        result = skills_install.install("codex", home=self.home)
        self.assertEqual(result["action"], "updated")
        self.assertTrue(result["backup"], "没有备份就走人了")
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertIn("我的手改版", Path(result["backup"]).read_text(encoding="utf-8"))
        self.assertEqual(dest.read_text(encoding="utf-8"), SKILL.read_text(encoding="utf-8"))

    def test_force_skips_backup(self) -> None:
        dest = self.home / ".dsh" / "skills" / skills_install.SKILL_NAME / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("旧内容\n", encoding="utf-8")
        result = skills_install.install("dsh", home=self.home, force=True)
        self.assertEqual(result["action"], "updated")
        self.assertFalse(result.get("backup"))
        self.assertEqual(list(dest.parent.glob("SKILL.md.bak-*")), [])

    def test_dry_run_touches_nothing(self) -> None:
        results = skills_install.install_all(home=self.home, dry_run=True)
        self.assertTrue(all(r["action"].startswith("would-") for r in results), results)
        self.assertFalse((self.home / ".dsh" / "skills").exists())

    def test_unknown_target_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            skills_install.target_dir("cursor", home=self.home)

    def test_mcp_snippet_is_valid_json_with_absolute_root(self) -> None:
        import json
        snippet = skills_install.mcp_snippet(["dsh"])
        payload = json.loads(snippet.split("\n\n")[0])
        server = payload["mcpServers"]["agent-team"]
        self.assertEqual(server["args"], ["-m", "agent_team.mcp_server"])
        self.assertEqual(server["cwd"], str(skills_install.workbench_root()),
                         "cwd 要是真路径，不能被转义成四重斜杠")


class CliEntryTests(unittest.TestCase):
    def test_cli_exposes_install_skills(self) -> None:
        self.assertIn("install-skills", known_subcommands())

    def test_cli_install_skills_help(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "agent_team.cli", "install-skills", "--help"],
                              capture_output=True, encoding="utf-8", errors="replace",
                              cwd=str(REPO), creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--dry-run", proc.stdout)


if __name__ == "__main__":
    unittest.main()