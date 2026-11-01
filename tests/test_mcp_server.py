"""MCP 工具（stdio JSON-RPC + CLI）的测试。

用隔离的 room（临时 AGENT_TEAM_HOME + 真 HTTP 服务）跑，不碰线上 8899。
重点钉住三件容易写错的事：
1. 协议：initialize / tools/list / tools/call / 未知方法 / 坏 JSON，一条坏消息不能带崩服务；
2. 语义：`POST /api/post` 只回消息 id，工具必须**等出任务号**，等不到要如实说；
3. 安全：令牌只走 Header；`/api/alive` 免令牌。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class IsolatedRoom:
    """临时 home 起一个真 room_web，返回端口与 token 文件路径。"""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "room").mkdir(parents=True, exist_ok=True)
        (self.root / "tasks").mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.token = "t" * 32
        (self.root / "room" / "web-token").write_text(self.token, encoding="utf-8")
        env = {**os.environ, "AGENT_TEAM_HOME": str(self.root)}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agent_team.room_web", "--port", str(self.port)],
            cwd=str(REPO), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1) as r:
                    if r.status == 200:
                        return
            except Exception:      # noqa: BLE001
                time.sleep(0.2)
        raise RuntimeError("隔离 room 没起来")

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:          # noqa: BLE001
            self.proc.kill()
        self.tmp.cleanup()


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RoomTestCase(unittest.TestCase):
    room: IsolatedRoom

    @classmethod
    def setUpClass(cls) -> None:
        cls.room = IsolatedRoom()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.room.close()

    def setUp(self) -> None:
        from agent_team import mcp_server
        self.mcp = mcp_server
        self._saved = (mcp_server.ROOT, mcp_server.TOKEN_FILE, mcp_server.DEFAULT_PORT)
        mcp_server.ROOT = self.room.root
        mcp_server.TOKEN_FILE = self.room.root / "room" / "web-token"
        mcp_server.DEFAULT_PORT = self.room.port

    def tearDown(self) -> None:
        self.mcp.ROOT, self.mcp.TOKEN_FILE, self.mcp.DEFAULT_PORT = self._saved


class ProtocolTests(unittest.TestCase):
    """纯协议层：不需要真服务。"""

    def setUp(self) -> None:
        from agent_team import mcp_server
        self.mcp = mcp_server

    def test_initialize_reports_server_info(self) -> None:
        out = self.mcp._handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(out["id"], 1)
        self.assertIn("protocolVersion", out["result"])
        self.assertIn("tools", out["result"]["capabilities"])
        self.assertEqual(out["result"]["serverInfo"]["name"], "agent-team")

    def test_initialized_notification_has_no_response(self) -> None:
        self.assertIsNone(self.mcp._handle_rpc(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_has_six_tools_with_schema(self) -> None:
        out = self.mcp._handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"]: t for t in out["result"]["tools"]}
        self.assertEqual(set(tools), {"team_status", "team_launch", "team_task_create",
                                      "team_task_wait", "team_task_list", "team_task_stop"})
        for tool in tools.values():
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["description"])
        self.assertIn("request", tools["team_task_create"]["inputSchema"]["required"])
        self.assertIn("task_id", tools["team_task_wait"]["inputSchema"]["required"])

    def test_no_suggest_tool(self) -> None:
        """外部方案的 #5「生成协作建议文本」被砍了：工具要返回事实，不替模型写字。"""
        self.assertNotIn("team_suggest_collab", self.mcp.TOOLS)

    def test_unknown_method_is_error(self) -> None:
        out = self.mcp._handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(out["error"]["code"], -32601)

    def test_tools_call_without_name_is_invalid_params(self) -> None:
        out = self.mcp._handle_rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                    "params": {}})
        self.assertEqual(out["error"]["code"], -32602)

    def test_ctrl_c_style_notification_is_ignored(self) -> None:
        self.assertIsNone(self.mcp._handle_rpc(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}))

    def test_serve_survives_bad_json_and_eof(self) -> None:
        """一条坏消息不能把服务带崩；EOF 正常退出。"""
        import io
        stdin = io.StringIO('not json\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n')
        captured = io.StringIO()
        real = sys.stdout
        sys.stdout = captured
        try:
            code = self.mcp.serve(stdin=stdin)
        finally:
            sys.stdout = real
        self.assertEqual(code, 0)
        lines = [json.loads(x) for x in captured.getvalue().splitlines() if x.strip()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1], {"jsonrpc": "2.0", "id": 9, "result": {}})


class StatusTests(RoomTestCase):
    def test_status_online_without_token(self) -> None:
        out = self.mcp.team_status()
        self.assertTrue(out["online"], out)
        self.assertIn("version", out)

    def test_status_offline_reports_why(self) -> None:
        out = self.mcp.team_status(port=_free_port())
        self.assertFalse(out["online"])
        self.assertIn("hint", out)

    def test_status_uses_alive_endpoint_not_guarded_one(self) -> None:
        src = (REPO / "agent_team" / "mcp_server.py").read_text(encoding="utf-8")
        self.assertIn('"/api/alive"', src)
        self.assertIn("token=False", src, "/api/alive 免令牌，别硬塞令牌")


class CreateTaskTests(RoomTestCase):
    def test_create_returns_task_id_when_worker_makes_card(self) -> None:
        """真实现场：发完消息后台会建卡。这里模拟 worker 在 2 秒后建卡。"""
        def make_card() -> None:
            time.sleep(2.0)
            tid = "T-261101-150000"
            tdir = self.room.root / "tasks" / tid
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "task.json").write_text(json.dumps({
                "id": tid, "title": "建一个计算器", "status": "planned",
                "rounds": [], "reviews": []}, ensure_ascii=False), encoding="utf-8")

        threading.Thread(target=make_card, daemon=True).start()
        out = self.mcp.team_task_create("建一个计算器，带加减乘除", plan_timeout=20)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["task_id"], "T-261101-150000")
        self.assertIn("message_id", out)

    def test_create_without_card_says_so_instead_of_faking_id(self) -> None:
        out = self.mcp.team_task_create("随便写个需求", plan_timeout=3)
        self.assertIsNone(out["task_id"], "等不到任务号就不能编一个")
        self.assertIn("hint", out)

    def test_create_rejects_empty_request(self) -> None:
        with self.assertRaises(self.mcp.ToolError):
            self.mcp.team_task_create("   ")

    def test_create_marks_client_identity(self) -> None:
        self.mcp.team_task_create("带身份的请求", client="claude-code", plan_timeout=1)
        import urllib.request
        token = (self.room.root / "room" / "web-token").read_text(encoding="utf-8").strip()
        req = urllib.request.Request(f"http://127.0.0.1:{self.room.port}/api/state")
        req.add_header("X-Team-Token", token)
        state = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
        rows = state.get("messages") or []
        mine = [r for r in rows if isinstance(r, dict) and "带身份的请求" in str(r.get("content"))]
        self.assertTrue(mine, "发出去的消息应该在群里")
        # agent 只认五角色（陌生名字会被折成 coordinator），身份记在 meta 上
        self.assertEqual(mine[-1].get("agent"), "user")
        self.assertEqual((mine[-1].get("meta") or {}).get("client"), "claude-code")
        self.assertEqual((mine[-1].get("meta") or {}).get("via"), "mcp")


class WaitTests(RoomTestCase):
    def _write_card(self, tid: str, status: str, **extra) -> None:
        tdir = self.room.root / "tasks" / tid
        tdir.mkdir(parents=True, exist_ok=True)
        payload = {"id": tid, "title": "t", "status": status, "rounds": [], "reviews": [],
                   **extra}
        (tdir / "task.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_wait_returns_terminal_result(self) -> None:
        tid = "T-261101-150001"
        self._write_card(tid, "passed", outcome="success",
                         review_wrote=[], out_of_scope=[])
        (self.room.root / "tasks" / tid / "report.md").write_text(
            "都跑通了，见 tasks/x/report.md", encoding="utf-8")
        out = self.mcp.team_task_wait(tid, timeout=10)
        self.assertEqual(out["status"], "passed")
        self.assertEqual(out["outcome"], "success")
        self.assertIn("都跑通了", out["report"])

    def test_wait_surfaces_reviewer_writes(self) -> None:
        """复核动过文件要能看见（0.9.13 的 review_wrote）。"""
        tid = "T-261101-150002"
        self._write_card(tid, "failed", review_wrote=["src/app.py"], out_of_scope=["tmp.log"])
        out = self.mcp.team_task_wait(tid, timeout=10)
        self.assertEqual(out["review_wrote"], ["src/app.py"])
        self.assertEqual(out["out_of_scope"], ["tmp.log"])

    def test_wait_unknown_task_raises(self) -> None:
        with self.assertRaises(self.mcp.ToolError):
            self.mcp.team_task_wait("T-999999-999999", timeout=2)

    def test_wait_timeout_is_not_an_error(self) -> None:
        tid = "T-261101-150003"
        self._write_card(tid, "dispatched")
        out = self.mcp.team_task_wait(tid, timeout=1, interval=0.3)
        self.assertTrue(out.get("timed_out"))
        self.assertEqual(out["status"], "dispatched")

    def test_wait_requires_task_id(self) -> None:
        with self.assertRaises(self.mcp.ToolError):
            self.mcp.team_task_wait("")


class AuthTests(unittest.TestCase):
    def test_token_never_goes_in_url(self) -> None:
        src = (REPO / "agent_team" / "mcp_server.py").read_text(encoding="utf-8")
        import re
        # 只查"把令牌拼进 URL"的写法；文档里提到 ?token= 是允许的
        self.assertIsNone(re.search(r"[?&]token=\{[^}]*\}", src),
                          "令牌只能进 X-Team-Token 头")
        self.assertIsNone(re.search(r"f\"[^\"]*token=", src))
        self.assertIn("X-Team-Token", src)

    def test_missing_token_file_gives_actionable_error(self) -> None:
        from agent_team import mcp_server
        saved = mcp_server.TOKEN_FILE
        mcp_server.TOKEN_FILE = Path(tempfile.gettempdir()) / "definitely-not-here" / "t"
        try:
            with self.assertRaises(mcp_server.ToolError) as ctx:
                mcp_server._token()
            self.assertIn("team_launch", str(ctx.exception))
        finally:
            mcp_server.TOKEN_FILE = saved

    def test_wrong_token_is_reported_not_swallowed(self) -> None:
        room = IsolatedRoom()
        try:
            from agent_team import mcp_server
            saved = (mcp_server.ROOT, mcp_server.TOKEN_FILE)
            mcp_server.ROOT = room.root
            mcp_server.TOKEN_FILE = Path(tempfile.gettempdir()) / "nope-token-file"
            try:
                status, data = mcp_server._request("GET", "/api/tasks", token="wrong",
                                                   port=room.port)
                self.assertEqual(status, 401)
                self.assertFalse(data.get("ok"))
            finally:
                mcp_server.ROOT, mcp_server.TOKEN_FILE = saved
        finally:
            room.close()


class StopTaskTests(RoomTestCase):
    """team_task_stop 走真服务：指令必须真的落进 room/control.json。

    stop 会先确认任务存在，所以这里先放一张真卡；不存在的任务号必须报错，
    绝不能写一条指向空气的停止指令、然后回一句"已停止"。
    """

    def _card(self, task_id: str, status: str = "running") -> Path:
        directory = self.room.root / "tasks" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "task.json").write_text(
            json.dumps({"id": task_id, "status": status, "title": "停止测试"}),
            encoding="utf-8")
        return directory

    def _control(self) -> dict:
        path = self.room.root / "room" / "control.json"
        if path.exists():
            path.unlink()
        return path

    def test_stop_writes_control_instruction(self) -> None:
        self._card("T-261101-120000")
        control_file = self._control()
        out = self.mcp.team_task_stop("T-261101-120000")
        self.assertTrue(out["ok"])
        self.assertEqual(out["task_id"], "T-261101-120000")
        self.assertEqual(out["status"], "running")
        data = json.loads(control_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("action"), "stop")
        self.assertEqual(data.get("task"), "T-261101-120000")

    def test_stop_via_rpc(self) -> None:
        self._card("T-261101-120001")
        self._control()
        out = self.mcp._handle_rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                    "params": {"name": "team_task_stop",
                                               "arguments": {"task_id": "T-261101-120001"}}})
        self.assertNotIn("error", out)
        self.assertFalse(out["result"].get("isError"), out["result"])

    def test_stop_unknown_task_raises_and_writes_nothing(self) -> None:
        control_file = self._control()
        with self.assertRaises(self.mcp.ToolError) as ctx:
            self.mcp.team_task_stop("T-261101-999999")
        self.assertIn("没有这个任务", str(ctx.exception))
        self.assertFalse(control_file.exists(), "不存在的任务不该留下停止指令")

    def test_stop_on_finished_task_writes_nothing(self) -> None:
        self._card("T-261101-120003", status="passed")
        control_file = self._control()
        out = self.mcp.team_task_stop("T-261101-120003")
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("already_finished"))
        self.assertEqual(out["status"], "passed")
        self.assertFalse(control_file.exists(), "已经结束的任务不需要停止指令")

    def test_stop_on_task_id_with_ampersand(self) -> None:
        """任务号带 `&` 也不能查错卡：不转义的话 `&x=2` 会被当成另一个查询参数。"""
        self._card("T-1&x=2")
        control_file = self._control()
        out = self.mcp.team_task_stop("T-1&x=2")
        self.assertTrue(out["ok"])
        data = json.loads(control_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("task"), "T-1&x=2")

    def test_stop_with_wrong_token_raises(self) -> None:
        self._card("T-261101-120002")
        token_file = self.room.root / "room" / "web-token"
        saved = token_file.read_text(encoding="utf-8")
        token_file.write_text("x" * 32, encoding="utf-8")
        try:
            with self.assertRaises(self.mcp.ToolError):
                self.mcp.team_task_stop("T-261101-120002")
        finally:
            token_file.write_text(saved, encoding="utf-8")


class TaskPathTests(unittest.TestCase):
    """任务号是调用方模型给的，不可控：拼进 URL 前必须整段转义。"""

    def test_task_path_encodes_unsafe_characters(self) -> None:
        from agent_team.mcp_server import _task_path
        self.assertEqual(_task_path("T-261101-120000"), "/api/task?id=T-261101-120000")
        self.assertEqual(_task_path("T-1&x=2"), "/api/task?id=T-1%26x%3D2")
        self.assertEqual(_task_path("a b"), "/api/task?id=a%20b")
        self.assertEqual(_task_path("a#b"), "/api/task?id=a%23b")


class CliTests(unittest.TestCase):
    def test_cli_lists_tools(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "agent_team.cli", "mcp", "--action", "list"],
                              capture_output=True, encoding="utf-8", errors="replace",
                              cwd=str(REPO),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("team_status", "team_launch", "team_task_create", "team_task_wait",
                     "team_task_list", "team_task_stop"):
            self.assertIn(name, proc.stdout)

    def test_cli_call_returns_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_team.cli", "mcp", "--action", "call",
             "--tool", "team_status", "--args", json.dumps({"port": _free_port()})],
            capture_output=True, encoding="utf-8", errors="replace", cwd=str(REPO),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["online"])

    def test_cli_unknown_tool_exits_nonzero_with_message(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_team.cli", "mcp", "--action", "call",
             "--tool", "nope", "--args", "{}"],
            capture_output=True, encoding="utf-8", errors="replace", cwd=str(REPO),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("没有这个工具", proc.stdout)

    def test_team_task_stop_tool_exists(self):
        """team_task_stop 工具已注册，inputSchema 正确。"""
        from agent_team.mcp_server import TOOLS
        self.assertIn("team_task_stop", TOOLS)
        spec = TOOLS["team_task_stop"]
        self.assertIn("fn", spec)
        self.assertIn("description", spec)
        self.assertIn("inputSchema", spec)
        schema = spec["inputSchema"]
        self.assertIn("task_id", schema["properties"])
        self.assertIn("task_id", schema["required"])

    def test_team_task_stop_requires_task_id(self):
        """team_task_stop 缺少 task_id 时报错。"""
        from agent_team.mcp_server import team_task_stop, ToolError
        with self.assertRaises(ToolError) as ctx:
            team_task_stop("")
        self.assertIn("缺少", str(ctx.exception))

    def test_team_task_create_session_id_exposed(self):
        """team_task_create 的 inputSchema 暴露了 session_id 参数。"""
        from agent_team.mcp_server import TOOLS
        schema = TOOLS["team_task_create"]["inputSchema"]
        self.assertIn("session_id", schema["properties"])
        # session_id 不是必须的
        self.assertNotIn("session_id", schema.get("required", []))


if __name__ == "__main__":
    unittest.main()