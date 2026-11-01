"""MCP 工具：把协作台暴露给任意支持 MCP 的 Agent（Claude Code / Codex / 豆包 / 本机大脑…）。

刻意只用标准库：MCP 的 stdio 传输就是「一行一个 JSON-RPC 2.0」，自己实现比引 SDK 更可控，
也不会因为对方的 SDK 升版把我们的工具带崩。同时保留 `agent-team mcp call` 的 CLI 入口，
没有 MCP 支持的客户端（或 shell 脚本）也能用同一套逻辑。

工具（按一次协作的调用顺序）：

    team_status     协作台在不在跑（不需要令牌）
    team_launch     没在跑就起来，返回一次性链接
    team_task_create 把需求发进群（协作台会拆卡执行），并**等出任务号**
    team_task_wait  阻塞等到终态，回报结果
    team_task_list  最近的任务卡

几个关键决定：

- **令牌只走 Header**：`X-Team-Token` 从 `room/web-token` 读，绝不进 URL
  （0.9.12 起带 `?token=` 的请求一律 401）。
- **`team_task_create` 必须返回任务号**：`POST /api/post` 只返回消息 id，
  任务卡是后台 worker 建的。所以这里会盯 `/api/messages?after=` 找任务号，
  等不到就如实说「已收到、还在规划」，而不是给一个假的 task_id。
- **不做「生成建议文本」这类工具**：那是调用方模型自己就能写的东西，
  工具应该返回事实（状态、任务号、结果），而不是替模型说话。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from .paths import workbench_root
from . import __version__

ROOT = workbench_root()
TOKEN_FILE = ROOT / "room" / "web-token"
DEFAULT_PORT = 8899
TERMINAL = {"passed", "failed", "exec-failed", "stopped"}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class ToolError(Exception):
    """工具没成功：message 会原样回给调用方模型，所以要写人话。"""


# ---------------------------------------------------------------- HTTP 基础
def _base(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or DEFAULT_PORT}"


def _task_path(task_id: str) -> str:
    """任务详情接口的路径。任务号来自调用方模型，是不可控字符串：必须整段转义。

    不转义的话，`T-1&x=2` 会被拆成两个参数、查到的不是这张卡（静默查错），
    带空格或 `#` 更直接让请求发不出去。任务号里合法的字符只有字母数字和 `-`。
    """
    return f"/api/task?id={quote(str(task_id), safe='')}"


def _token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ToolError(f"读不到访问令牌（{TOKEN_FILE}）：协作台还没起过？先 team_launch") from exc


def _request(method: str, path: str, body: dict | None = None,
             token: str | None = None, timeout: float = 10.0,
             port: int | None = None) -> tuple[int, dict]:
    url = _base(port) + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not False:
        req.add_header("X-Team-Token", token if isinstance(token, str) else _token())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return exc.code, {"error": raw[:400]}
    except (urllib.error.URLError, OSError) as exc:
        raise ToolError(f"连不上协作台（{url}）：{exc}") from exc


# ---------------------------------------------------------------- 工具实现
def _rows(payload) -> list:
    """`/api/tasks` 直接回列表，`/api/messages` 有时包成 {"messages": [...]}——两种都认。"""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("tasks", "messages"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def _card(tid: str) -> dict:
    """任务卡原文件：接口不暴露的字段（outcome / out_of_scope / review_wrote）在这儿。"""
    try:
        text = (ROOT / "tasks" / tid / "task.json").read_text(encoding="utf-8")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def team_status(port: int | None = None) -> dict:
    """协作台在不在跑。`/api/alive` 是唯一免令牌的接口。"""
    try:
        status, data = _request("GET", "/api/alive", token=False, timeout=3, port=port)
    except ToolError:
        out = {"online": False, "url": _base(port) + "/",
               "hint": "协作台没在跑，用 team_launch 起来"}
        out.update(_last_stop())
        return out
    alive = status == 200 and bool(data.get("ok"))
    out = {"online": alive, "url": _base(port) + "/"}
    if alive:
        out["version"] = data.get("version")
        try:
            _, tasks = _request("GET", "/api/tasks", port=port)
            rows = _rows(tasks)
            if rows:
                out["recent_tasks"] = [
                    {"id": t.get("id"), "title": t.get("title"), "status": t.get("status")}
                    for t in rows[:5]]
        except ToolError:
            pass
    else:
        out["hint"] = "端口上有别的服务，或协作台正在启动"
    out["token_source"] = str(TOKEN_FILE)
    return out


def _last_stop() -> dict:
    """上次为什么停的：房间里的 session_state.json 记了原因，比"连不上"有用得多。"""
    try:
        data = json.loads((ROOT / "room" / "session_state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    state = str(data.get("collab") or "")
    reason = str(data.get("reason") or "")
    if state != "ended" or not reason:
        return {}
    return {"last_stop": {"reason": reason, "at": data.get("at"),
                          "note": "如果这是因为网页关了收工，纯 agent 用法不受影响："
                                  "从没人开过页面的服务不会自己关"}}


def team_launch(port: int | None = None, wait: float = 30.0) -> dict:
    """没在跑就起，已经在跑就不动；返回一次性链接（浏览器用）。"""
    from . import launcher

    port = port or DEFAULT_PORT
    if launcher.page_ready(port):
        return {"ok": True, "started": False, "url": launcher.one_time_url(port),
                "note": "本来就在跑，没有重启（重启会打断正在执行的任务）"}
    if launcher.port_open(port):
        raise ToolError(f"端口 {port} 被别的程序占着，页面打不开；换端口或先清掉占用的进程")
    code = launcher.start_server(port)
    if code != 0:
        raise ToolError(f"启动命令失败（退出码 {code}），看 room/worker.log")
    if not launcher.wait_ready(port, wait):
        raise ToolError(f"等了 {wait:.0f} 秒页面还没就绪，看 room/worker.log")
    return {"ok": True, "started": True, "url": launcher.one_time_url(port),
            "note": "一次性链接，打开后浏览器会记住；别外传"}


def _newest_task_id(port: int | None) -> str:
    try:
        _, data = _request("GET", "/api/tasks", port=port)
    except ToolError:
        return ""
    for row in _rows(data):
        if row.get("id"):
            return str(row["id"])
    return ""


def _card_status(tid: str, port: int | None = None) -> str:
    try:
        _, data = _request("GET", _task_path(tid), port=port)
        detail = data.get("task") if isinstance(data.get("task"), dict) else data
        return str(detail.get("status") or "")
    except ToolError:
        return ""


def _recent_messages(port: int | None) -> list:
    """当前会话的消息（和页面同一个来源）。

    `/api/messages?after=<n>` 走的是旧账本，新会话存储下返回空——别用它做增量，
    直接读 `/api/state` 的 messages 更稳（页面也是这么读的）。
    """
    _, data = _request("GET", "/api/state", port=port)
    return _rows(data.get("messages") if isinstance(data, dict) else [])


def team_task_create(request: str, port: int | None = None, client: str = "mcp",
                     plan_timeout: float = 90.0, session_id: str = "") -> dict:
    """把需求发进群，并尽量等到任务号。

    `POST /api/post` 只返回消息 id；任务卡由后台 worker 拆好后发一条带 task 的群消息。
    这里先记下发之前的"最新任务 id"，发完轮询 `/api/messages?after=` 找任务号。
    """
    request = (request or "").strip()
    if not request:
        raise ToolError("需求不能为空")
    before_task = _newest_task_id(port)
    # agent 只认五角色/user/system（`_agent_from` 会把没见过的名字折成 coordinator），
    # 所以"谁提的需求"放 meta：它原样保留在群消息里，页面也能看到。
    body = {"content": request, "agent": "user",
            "meta": {"client": client, "via": "mcp"}}
    if session_id:
        body["session_id"] = session_id
    status, msg = _request("POST", "/api/post", body, port=port)
    if status != 200 or not msg.get("ok"):
        raise ToolError(f"消息没发出去（HTTP {status}）：{msg.get('error') or msg}")
    message_id = str(msg.get("id") or "")

    result: dict = {"ok": True, "message_id": message_id, "task_id": None}
    deadline = time.monotonic() + max(0.0, plan_timeout)
    while time.monotonic() < deadline:
        try:
            rows = _recent_messages(port)
        except ToolError:
            time.sleep(1.0)
            continue
        # 消息 id 是 "msg-0001" 这种字符串，不是数字，所以按位置找自己那条再往后扫
        start = 0
        for index, row in enumerate(rows):
            if str(row.get("id") or "") == message_id:
                start = index + 1
        for row in rows[start:]:
            tid = row.get("task_id") or (row.get("meta") or {}).get("task")
            if tid:
                result["task_id"] = str(tid)
                result["plan_message"] = str(row.get("content") or "")[:300]
                result["status"] = _card_status(str(tid), port)
                return result
        newest = _newest_task_id(port)
        if newest and newest != before_task:
            result["task_id"] = newest
            result["status"] = _card_status(newest, port)
            return result
        time.sleep(1.0)
    result["hint"] = ("消息已收下，但还没等到任务号（规划中或需要你确认）。"
                      "稍后用 team_task_list 看新卡，或再调 team_task_wait")
    return result


def team_task_wait(task_id: str, port: int | None = None, timeout: float = 1800.0,
                   interval: float = 2.0) -> dict:
    """阻塞到终态（passed/failed/exec-failed/stopped），把结果一次说清。

    `/api/task` 给的是**扁平 detail**（status/rounds/reviews/artifacts/证据尾巴），
    接口不暴露 `outcome`/`out_of_scope`/`review_wrote`，所以这三项从任务卡原文件补；
    报告正文在 `tasks/<id>/report.md`。
    """
    task_id = (task_id or "").strip()
    if not task_id:
        raise ToolError("要等哪个任务？给 task_id（team_task_create 会返回）")
    deadline = time.monotonic() + max(0.0, timeout)
    last = ""
    while True:
        status, data = _request("GET", _task_path(task_id), port=port)
        if status == 404:
            raise ToolError(f"没有这个任务：{task_id}")
        detail = data.get("task") if isinstance(data.get("task"), dict) else data
        state = str(detail.get("status") or "")
        if state:
            last = state
        if state in TERMINAL:
            card = _card(task_id)
            reviews = detail.get("reviews") or card.get("reviews") or []
            wrote = card.get("review_wrote")
            if not isinstance(wrote, list):
                wrote = [r.get("wrote") for r in reviews
                         if isinstance(r, dict) and r.get("wrote")]
            report = ""
            try:
                report = (ROOT / "tasks" / task_id / "report.md").read_text(
                    encoding="utf-8", errors="replace")[:2000]
            except OSError:
                pass
            return {
                "task_id": task_id, "status": state,
                "title": detail.get("title"),
                "outcome": card.get("outcome") or detail.get("outcome"),
                "out_of_scope": card.get("out_of_scope") or [],
                "review_wrote": [w for w in wrote if w][:20],
                "rounds": len(detail.get("rounds") or []),
                "verify_tail": detail.get("verify_tail") or "",
                "review_text": detail.get("review_text") or "",
                "report": report,
                "artifacts": [a.get("path") for a in (detail.get("artifacts") or [])
                              if isinstance(a, dict) and a.get("path")],
                "workspace": detail.get("workspace"),
            }
        if time.monotonic() >= deadline:
            return {"task_id": task_id, "status": last or "unknown", "timed_out": True,
                    "hint": f"等了 {timeout:.0f} 秒还没结束；任务可能还在跑，稍后再 wait 一次"}
        time.sleep(max(0.2, interval))


def team_task_list(limit: int = 10, port: int | None = None) -> dict:
    _, data = _request("GET", "/api/tasks", port=port)
    rows = _rows(data)[:max(1, int(limit))]
    return {"tasks": [{"id": r.get("id"), "title": r.get("title"), "status": r.get("status"),
                       "updated_at": r.get("updated_at")} for r in rows]}


def team_task_stop(task_id: str, port: int | None = None) -> dict:
    """停止一个正在执行的任务。

    调用 `POST /api/control {"action":"stop","task_id":"..."}` 向引擎发送停止指令，
    引擎会在下一个步骤边界停下，任务记为 `stopped`。

    先确认任务真的存在：`/api/control` 不校验任务号，写进去的指令如果指向不存在的卡，
    就会一直躺在 `room/control.json` 里没人消费——那等于对调用方撒了个"已停止"的谎。
    """
    task_id = (task_id or "").strip()
    if not task_id:
        raise ToolError("缺少 task_id")

    code, data = _request("GET", _task_path(task_id), port=port)
    if code == 404:
        raise ToolError(f"没有这个任务：{task_id}（先用 team_task_list 确认任务号）")
    detail = data.get("task") if isinstance(data.get("task"), dict) else data
    state = str(detail.get("status") or "")
    if state in TERMINAL:
        return {"ok": True, "task_id": task_id, "status": state, "already_finished": True,
                "message": f"任务已经是 {state}，没有写停止指令"}

    body = {"action": "stop", "task_id": task_id}
    code, data = _request("POST", "/api/control", body, port=port)
    if code != 200 or not data.get("ok"):
        raise ToolError(f"停止任务失败：{data.get('error') or f'HTTP {code}'}")

    return {
        "ok": True,
        "task_id": task_id,
        "action": "stop",
        "status": state,
        "message": "停止指令已发送，引擎会在下一个步骤边界停下",
        "at": data.get("at"),
    }


TOOLS: dict[str, dict] = {
    "team_status": {
        "fn": team_status,
        "description": "协作台在不在跑（不需要令牌）。返回 online、地址、最近的几张任务卡。",
        "inputSchema": {"type": "object", "properties": {
            "port": {"type": "integer", "description": "端口，默认 8899"}}},
    },
    "team_launch": {
        "fn": team_launch,
        "description": "协作台没在跑就启动，已经在跑就不动（不重启，避免打断任务）。返回一次性打开链接。",
        "inputSchema": {"type": "object", "properties": {
            "port": {"type": "integer", "description": "端口，默认 8899"},
            "wait": {"type": "number", "description": "最多等多少秒就绪，默认 30"}}},
    },
    "team_task_create": {
        "fn": team_task_create,
        "description": ("把需求发进协作台的群，由它拆任务卡、执行、验收。返回 task_id（等得到时）"
                        "或 message_id + 提示。需求要写成一句能独立看懂的话。"),
        "inputSchema": {"type": "object", "properties": {
            "request": {"type": "string", "description": "需求原文"},
            "client": {"type": "string", "description": "调用方标识，会记在群消息上，默认 mcp"},
            "session_id": {"type": "string", "description": "指定会话 ID（可选，不指定就用当前活动会话）"},
            "plan_timeout": {"type": "number", "description": "等任务号最多多少秒，默认 90"},
            "port": {"type": "integer"}}, "required": ["request"]},
    },
    "team_task_wait": {
        "fn": team_task_wait,
        "description": "等一个任务到终态（passed/failed/exec-failed/stopped），返回结论、越界改动、复核是否动过文件、报告。",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "任务号，如 T-261101-120000"},
            "timeout": {"type": "number", "description": "最多等多少秒，默认 1800"},
            "port": {"type": "integer"}}, "required": ["task_id"]},
    },
    "team_task_list": {
        "fn": team_task_list,
        "description": "最近的协作任务卡（倒序）。",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "条数，默认 10"},
            "port": {"type": "integer"}}},
    },
    "team_task_stop": {
        "fn": team_task_stop,
        "description": "停止一个正在执行的任务。引擎会在下一个步骤边界停下，任务记为 stopped。",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "任务号，如 T-261101-120000"},
            "port": {"type": "integer"}}, "required": ["task_id"]},
    },
}


def call_tool(name: str, arguments: dict | None = None) -> dict:
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolError(f"没有这个工具：{name}；可用：{', '.join(TOOLS)}")
    args = dict(arguments or {})
    try:
        return spec["fn"](**args)
    except TypeError as exc:
        raise ToolError(f"参数不对：{exc}") from exc
    except ToolError:
        raise
    except Exception as exc:      # noqa: BLE001 - 工具内部异常也要变成人话回给模型
        raise ToolError(f"{name} 失败：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- MCP stdio
def _reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_rpc(msg: dict) -> dict | None:
    """一条 JSON-RPC 消息 → 一条响应（通知返回 None）。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or "2024-11-05",
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "agent-team", "version": __version__}})
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": s["description"],
                              "inputSchema": s["inputSchema"]} for n, s in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return err(-32602, "tools/call 需要 name")
        try:
            payload = call_tool(name, params.get("arguments") or {})
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if msg_id is None:          # 其它通知：静默忽略，别把 stdout 搞脏
        return None
    return err(-32601, f"不支持的方法：{method}")


def serve(stdin=None, stdout=None) -> int:
    """stdio 服务：一行一条 JSON-RPC。知道 EOF 才退。"""
    src = stdin or sys.stdin
    for line in src:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 解析失败"}})
            continue
        if not isinstance(msg, dict):
            _reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "要一个对象"}})
            continue
        try:
            response = _handle_rpc(msg)
        except Exception as exc:      # noqa: BLE001 - 单条坏消息不能把服务带崩
            response = {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32603, "message": f"内部错误：{exc}"}}
        if response is not None:
            _reply(response)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-team mcp",
                                 description="MCP 工具服务（stdio）；也可用 call 直接调工具")
    ap.add_argument("--action", choices=["serve", "list", "call"], default="serve")
    ap.add_argument("--tool", default="", help="action=call 时指定工具名")
    ap.add_argument("--args", default="{}", help="action=call 时的 JSON 参数")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if args.action == "serve":
        return serve()
    if args.action == "list":
        for name, spec in TOOLS.items():
            print(f"{name}\n  {spec['description']}")
        return 0
    if not args.tool:
        print("要指定 --tool，例如：--tool team_status --args '{}'")
        return 2
    try:
        payload = call_tool(args.tool, json.loads(args.args or "{}"))
    except ToolError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())