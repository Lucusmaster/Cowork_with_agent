"""产物相关：工作区里留下的空临时目录、以及「产物从哪看」。

用户报的两件事，对应两组断言：
1. Codex 跑完在工作区堆一堆 `.tmpXXXXXX` 空目录 → `prune_scratch` 收掉它们（只删空目录）；
2. 产出的文件在网页上找不到 → 任务卡/消息里要有 `workspace:` 引用（蓝色文件名），
   并且越界的那些要单独标出来。
"""
from __future__ import annotations

import json
import inspect
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import codex_bridge, room_web, team


class PruneScratchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prunes_empty_dirs_and_the_scratch_dir_itself(self) -> None:
        scratch = self.ws / ".tmp"
        for name in (".tmpAAAAAA", ".tmpBBBBBB", ".tmpCCCCCC"):
            (scratch / name).mkdir(parents=True)
        removed = codex_bridge.prune_scratch(self.ws)
        self.assertEqual(removed, 3)
        self.assertFalse(scratch.exists(), "空干净了就该把 .tmp 一起收掉，别留个空壳")

    def test_keeps_files_inside_scratch(self) -> None:
        scratch = self.ws / ".tmp"
        (scratch / ".tmpAAAAAA").mkdir(parents=True)
        (scratch / "codex-1.out.txt").write_text("日志", encoding="utf-8")
        removed = codex_bridge.prune_scratch(self.ws)
        self.assertEqual(removed, 1)
        self.assertTrue((scratch / "codex-1.out.txt").is_file(), "有文件就说明本轮还在用，不能删")

    def test_keeps_non_empty_dirs(self) -> None:
        scratch = self.ws / ".tmp"
        busy = scratch / ".tmpBUSY"
        busy.mkdir(parents=True)
        (busy / "payload").write_text("x", encoding="utf-8")
        self.assertEqual(codex_bridge.prune_scratch(self.ws), 0)
        self.assertTrue(busy.is_dir())

    def test_missing_scratch_is_not_an_error(self) -> None:
        self.assertEqual(codex_bridge.prune_scratch(self.ws), 0)
        self.assertEqual(codex_bridge.prune_scratch(None), 0)

    def test_prune_never_touches_workspace_products(self) -> None:
        (self.ws / "pelican_bike.html").write_text("<html>", encoding="utf-8")
        (self.ws / ".tmp").mkdir()
        codex_bridge.prune_scratch(self.ws)
        self.assertTrue((self.ws / "pelican_bike.html").is_file(), "产物一个字节都不能动")


class _GitWorkspace(unittest.TestCase):
    """真起一个 git 仓库：产物判定靠 `git status`，不能拿假数据糊弄。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        (self.ws / ".gitignore").write_text("__pycache__/\n.tmp/\n", encoding="utf-8")
        (self.ws / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=str(self.ws), capture_output=True,
            encoding="utf-8", errors="replace")
        return proc.stdout or ""


class WorkspaceProductsTests(_GitWorkspace):
    def test_lists_generated_file_as_product(self) -> None:
        (self.ws / "pelican_bike.html").write_text("<svg/>", encoding="utf-8")
        products = team.workspace_products({"workspace": str(self.ws), "scope": ["pelican_bike.html"]})
        self.assertEqual([p["path"] for p in products], ["pelican_bike.html"])
        self.assertEqual(products[0]["ref"], "workspace:pelican_bike.html")
        self.assertTrue(products[0]["in_scope"])

    def test_marks_out_of_scope_but_still_lists_it(self) -> None:
        (self.ws / "pelican_bike.html").write_text("<svg/>", encoding="utf-8")
        (self.ws / "notes.md").write_text("顺手写的\n", encoding="utf-8")
        products = team.workspace_products({"workspace": str(self.ws), "scope": ["pelican_bike.html"]})
        by_path = {p["path"]: p["in_scope"] for p in products}
        self.assertTrue(by_path["pelican_bike.html"])
        self.assertFalse(by_path["notes.md"], "清单外的要标出来，但照样列（用户选的是提醒不删）")

    def test_no_scope_means_everything_is_in_scope(self) -> None:
        (self.ws / "a.txt").write_text("a", encoding="utf-8")
        products = team.workspace_products({"workspace": str(self.ws)})
        self.assertEqual(len(products), 1)
        self.assertTrue(products[0]["in_scope"])

    def test_ledger_and_junk_are_not_products(self) -> None:
        (self.ws / ".tmp").mkdir()
        (self.ws / "__pycache__").mkdir()
        (self.ws / "__pycache__" / "x.pyc").write_bytes(b"0")
        (self.ws / "build.log").write_text("log", encoding="utf-8")
        self.assertEqual(team.workspace_products({"workspace": str(self.ws), "scope": []}), [])

    def test_does_not_run_on_a_non_repo_directory(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            (Path(plain) / "x.txt").write_text("x", encoding="utf-8")
            self.assertEqual(team.workspace_products({"workspace": plain}), [])


class TaskProductsApiTests(_GitWorkspace):
    """右栏的任务卡要能拿到产物，否则「找不到产物」这条就没修。"""

    def test_detail_exposes_products_with_workspace_ref(self) -> None:
        # room_web 用的是自己的实现（不 import team，免得网页进程背上任务内核）
        (self.ws / "pelican_bike.html").write_text("<svg/>", encoding="utf-8")
        products = room_web._task_products({"workspace": str(self.ws), "scope": ["pelican_bike.html"]})
        self.assertEqual(products[0]["ref"], "workspace:pelican_bike.html")
        self.assertTrue(products[0]["in_scope"])

    def test_artifact_api_can_read_a_workspace_product(self) -> None:
        (self.ws / "pelican_bike.html").write_text("<svg>鹈鹕</svg>", encoding="utf-8")
        with mock.patch.object(room_web, "_project_workspace", lambda: str(self.ws)):
            payload = room_web._artifact_payload("workspace:pelican_bike.html")
        self.assertTrue(payload["ok"], payload)
        self.assertIn("鹈鹕", payload["content"])

    def test_artifact_api_refuses_a_path_outside_the_workspace(self) -> None:
        with mock.patch.object(room_web, "_project_workspace", lambda: str(self.ws)):
            payload = room_web._artifact_payload("workspace:../secrets.txt")
        self.assertFalse(payload["ok"])


class PageContractProductsTests(unittest.TestCase):
    """页面上真的挂上了：蓝色文件名、右键菜单、两个系统动作接口。"""

    def test_products_render_as_clickable_file_names(self) -> None:
        page = room_web.PAGE
        self.assertIn("function artifactChip(", page)
        self.assertIn("btn.oncontextmenu", page, "产物要能右击出菜单")
        self.assertIn("openArtMenu", page)
        self.assertIn("清单外", page, "越界产物要标出来")

    def test_menu_offers_open_reveal_and_copy(self) -> None:
        page = room_web.PAGE
        self.assertIn('id="art-menu"', page)
        for act in ("preview", "open", "reveal", "copy"):
            self.assertIn(f'data-act="{act}"', page, f"右键菜单缺 {act}")

    def test_server_routes_for_open_and_reveal_exist(self) -> None:
        routes = inspect.getsource(room_web.Handler._route_post)
        self.assertIn("/api/artifact/reveal", routes)
        self.assertIn("/api/artifact/open", routes)
        self.assertIn("_post_artifact_action", routes)

    def test_changes_panel_uses_the_same_chips(self) -> None:
        page = room_web.PAGE
        self.assertIn("chipHtml('workspace:' + name", page,
                      "右栏「代码改动」也要点得开，不能还是一串死路径")


class OpenInSystemTests(unittest.TestCase):
    def test_reveal_spawns_explorer_with_quiet_flags(self) -> None:
        with mock.patch.object(room_web.subprocess, "Popen") as popen:
            ok, why = room_web._open_in_system(r"C:\w\a.html", "reveal")
        self.assertTrue(ok, why)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], "explorer")
        self.assertIn("/select,", args[0])
        self.assertIn("creationflags", kwargs, "不能弹黑框")

    def test_open_files_a_missing_path_is_reported(self) -> None:
        with mock.patch.object(room_web.os, "startfile", create=True,
                               side_effect=OSError("没有关联程序")), \
             mock.patch.object(room_web.os, "name", "nt"):
            ok, why = room_web._open_in_system(r"C:\w\a.bin", "open")
        self.assertFalse(ok)
        self.assertIn("打不开", why)


class WorkspaceProductsRouteTests(_GitWorkspace):
    """a> 端到端：任务卡的 artifacts 里，产物排在证据前面。"""

    def test_products_come_first_in_artifacts(self) -> None:
        tid = "T-990101-000000"
        tdir = room_web.TASKS / tid
        tdir.mkdir(parents=True, exist_ok=True)
        try:
            (self.ws / "out.html").write_text("<svg/>", encoding="utf-8")
            (tdir / "task.json").write_text(json.dumps({
                "id": tid, "title": "t", "goal": "g", "status": "passed",
                "scope": ["out.html"], "workspace": str(self.ws),
                "acceptance": [], "verify": [], "rounds": [], "reviews": [],
            }), encoding="utf-8")
            (tdir / "report.md").write_text("# 报告", encoding="utf-8")
            detail = room_web._task_detail(tid)
            self.assertIsNotNone(detail)
            paths = [a["path"] for a in detail["artifacts"]]
            self.assertEqual(paths[0], "workspace:out.html")
            self.assertIn("report.md", paths[0] + " ".join(paths))
            self.assertEqual(detail["products"][0]["path"], "out.html")
        finally:
            if tdir.is_dir():
                for f in tdir.iterdir():
                    f.unlink()
                tdir.rmdir()


if __name__ == "__main__":
    unittest.main()