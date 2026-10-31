import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 包在仓库根
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent_team import worker  # noqa: E402


class ChatFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.tasks, self.room, self.proj = base / "tasks", base / "room", base / "proj"
        for directory in (self.tasks, self.room, self.proj):
            directory.mkdir(parents=True)
        self.saved = (worker.TASKS, worker.ROOM, worker.ROOT, worker.PROJECT_FILE)
        worker.TASKS, worker.ROOM, worker.ROOT = self.tasks, self.room, base
        worker.PROJECT_FILE = self.room / "project.json"
        self.said: list = []
        self.posts: list = []
        self._real_post = worker.room.post
        self._patched_fns = {name: getattr(worker, name)
                            for name in ("detect_workspace", "brain_decide", "run_card")}
        worker.room.post = lambda author, text, kind="msg", meta=None: self.posts.append((author, text))
        worker.say = lambda text: self.said.append(text)
        worker.event = lambda *a, **k: None

    def tearDown(self):
        for name, fn in self._patched_fns.items():
            setattr(worker, name, fn)
        worker.room.post = self._real_post
        worker.TASKS, worker.ROOM, worker.ROOT, worker.PROJECT_FILE = self.saved
        self.tmp.cleanup()

    def test_vague_message_without_project_asks_for_workspace(self):
        worker.brain_decide = lambda req, ws=None: {
            "say": "工作区是哪个目录？给我绝对路径我就开工。", "action": "chat"}
        worker.handle("把页面弄好看点")
        self.assertTrue(any("工作区是哪个目录" in text for _, text in self.posts))

    def test_set_project_persists(self):
        worker.brain_decide = lambda req, ws=None: {
            "say": "记下了，项目绑到 proj。", "action": "set_project",
            "workspace": str(self.proj), "project_name": "演示项目"}
        worker.detect_workspace = lambda text: (self.proj, "")
        worker.handle("我们做一个演示项目，工作区在这儿")
        data = json.loads(worker.PROJECT_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["workspace"], str(self.proj))
        self.assertEqual(data["name"], "演示项目")

    def test_run_creates_card_and_publishes_voices(self):
        calls: list = []
        worker.detect_workspace = lambda text: (self.proj, "")
        worker.run_card = lambda card, ws, req: calls.append((card["title"], str(ws)))
        worker.brain_decide = lambda req, ws=None: {
            "say": "我按「加导出」理解，先做最小可用的。", "action": "run",
            "task": {"title": "加导出", "goal": "导出 csv", "acceptance": ["能导出"],
                     "verify": ["python -m pytest -q"], "scope": ["export.py"]}}
        worker.handle("把数据导出")
        self.assertEqual(calls, [("加导出", str(self.proj))])


class ThreadContextTests(unittest.TestCase):
    def test_thread_text_includes_everyone(self):
        messages = [
            {"id": 1, "from": "user", "kind": "task", "text": "做一个登录页"},
            {"id": 2, "from": "deepseek", "kind": "msg", "text": "好，先做表单"},
            {"id": 3, "from": "codex", "kind": "result", "text": "表单已完成"},
            {"id": 4, "from": "reviewer", "kind": "result", "text": "PASS"},
            {"id": 5, "from": "system", "kind": "event", "text": "流水线事件"},
        ]
        saved = worker.room.read_all
        worker.room.read_all = lambda: messages
        try:
            text = worker.thread_text()
        finally:
            worker.room.read_all = saved
        self.assertIn("用户: 做一个登录页", text)
        self.assertIn("DeepSeek(大脑): 好，先做表单", text)
        self.assertIn("Codex(执行): 表单已完成", text)
        self.assertIn("Codex复核: PASS", text)
        self.assertNotIn("流水线事件", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)