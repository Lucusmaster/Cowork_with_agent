#!/usr/bin/env python3
"""群聊消息总线（过渡期兼容层）。

新代码请直接用 ``agent_team.session`` 写新消息结构（id/session_id/role/agent/
content/artifacts/status/created_at）。这个模块保留旧接口，让 ``worker.py``、
``room_web.py`` 这些还没改完的调用点继续能跑：

  python -m agent_team.room post --as user --text "帮我做 X"     # 发消息
  python -m agent_team.room post --as codex --text "..." --kind result
  python -m agent_team.room tail -n 20                           # 看最近消息
  python -m agent_team.room since --after 12 [--json]            # 看 12 之后的新消息
  python -m agent_team.room pending                              # 看还没被大脑认领的用户任务
  python -m agent_team.room ack --id 12                          # 标记已认领
  python -m agent_team.room wait --after 12 --timeout 60         # 阻塞等新消息

``post()`` 写的是新消息结构（内部调 ``session.append``），``read_all()`` 则把
各会话消息映射回旧格式（整数 id、from/kind/text），所以旧调用点不用改也能跑。
``room/messages.jsonl`` 不再追加，只作为旧账本由 ``session.py`` 迁移读取。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import session

ROOT = Path(__file__).resolve().parent.parent
ROOM = ROOT / "room"
MESSAGES = ROOM / "messages.jsonl"      # 旧账本：只读（内容已迁移到 sessions/legacy.jsonl）
STATE = ROOM / "state.json"

# Windows 控制台默认 GBK，表情符号会炸；统一按 UTF-8 输出（不可编码字符降级处理）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 成员显示名：新角色名 + 旧成员名（兼容别名）
MEMBERS = {
    "user": "我",
    "coordinator": "协调",
    "planner": "规划",
    "executor": "执行",
    "tester": "验收",
    "reviewer": "复核",
    "system": "调度",
    # 旧成员名保留，映射到新角色（deepseek → coordinator）
    "deepseek": "协调",
    "codex": "执行",
    "relay": "人工转发",
}

# 旧成员名 → 新 agent 角色；没列出的（新角色名本身）原样使用
ALIASES = {
    "deepseek": "coordinator",
    "codex": "executor",
    "relay": "coordinator",
}

# 这些键是我们的内部记账字段，映射回旧格式时不往 meta 里塞
_INTERNAL_META = ("from", "kind", "name", "legacy_id")


def _ensure() -> None:
    ROOM.mkdir(parents=True, exist_ok=True)
    if not MESSAGES.exists():
        MESSAGES.write_text("", encoding="utf-8")
    if not STATE.exists():
        STATE.write_text(json.dumps({"last_handled_id": 0}), encoding="utf-8")


def _read_state() -> dict:
    _ensure()
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"last_handled_id": 0}


def _write_state(state: dict) -> None:
    _ensure()
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def agent_for(author: str) -> str:
    """旧成员名 → 新 agent 角色（deepseek→coordinator、codex→executor…）。"""
    name = str(author or "").strip() or "user"
    return ALIASES.get(name, session.AGENT_ALIASES.get(name, name))


def role_for(author: str) -> str:
    """旧成员名 → 消息 role。"""
    name = str(author or "").strip()
    if name == "system":
        return "system"
    if name == "user":
        return "user"
    return "assistant"


def post(author: str, text: str, kind: str = "msg",
         meta: dict | None = None, session_id: str | None = None) -> dict:
    """发一条消息：落到新结构，返回值保留旧格式字段并附带新字段。

    meta 里这几个键会被提升到新消息结构：
    - ``artifacts`` → 消息的 artifacts；
    - ``status`` → 消息的 status（缺省 complete）；
    - ``task`` / ``task_id`` → 消息的 task_id。
    """
    _ensure()
    name = str(author or "").strip() or "user"
    payload = dict(meta or {}) if isinstance(meta, dict) else {}
    artifacts = payload.get("artifacts") or []
    if isinstance(artifacts, str):
        artifacts = [artifacts]
    task_id = payload.get("task") or payload.get("task_id") or None
    sid = str(session_id or "").strip() or session.active_session()["id"]

    message = session.append(
        sid,
        role=role_for(name),
        agent=agent_for(name),
        content=str(text or ""),
        task_id=str(task_id) if task_id else None,
        artifacts=[str(item) for item in artifacts],
        status=str(payload.get("status") or "complete"),
        meta={"from": name, "kind": str(kind or "msg"), **payload},
    )
    view = _legacy_view(message, _legacy_id_of(message))
    view.update({
        "message_id": message.get("id"),
        "session_id": message.get("session_id"),
        "role": message.get("role"),
        "agent": message.get("agent"),
        "task_id": message.get("task_id"),
        "content": message.get("content"),
        "artifacts": list(message.get("artifacts") or []),
        "status": message.get("status"),
        "created_at": message.get("created_at"),
    })
    return view


def _all_messages() -> list[dict]:
    """所有会话的消息按时间拼成一条流水（旧账本最早，自然排在最前）。"""
    collected = []
    for entry in session.sessions():
        sid = str(entry.get("id") or "")
        if not sid:
            continue
        for position, message in enumerate(session.messages(sid, limit=0)):
            collected.append((str(message.get("created_at") or ""), sid, position, message))
    collected.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in collected]


def _infer_kind(message: dict) -> str:
    meta = message.get("meta")
    if isinstance(meta, dict) and meta.get("kind"):
        return str(meta["kind"])
    role = str(message.get("role") or "")
    if role == "user":
        return "task"          # 旧 worker 只认 from=user 且 kind in (task, msg)
    if role == "system" or str(message.get("agent") or "") == "system":
        return "event"
    return "result"


def _legacy_view(message: dict, legacy_id: int) -> dict:
    """新消息 → 旧格式（整数 id、from/name/kind/text/meta）。"""
    meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
    author = str(meta.get("from") or message.get("agent") or "")
    view = {
        "id": int(legacy_id),
        "at": str(message.get("created_at") or ""),
        "from": author,
        "name": MEMBERS.get(author, author),
        "kind": _infer_kind(message),
        "text": str(message.get("content") or ""),
    }
    public = {key: value for key, value in meta.items() if key not in _INTERNAL_META}
    if public:
        view["meta"] = public
    return view


def _legacy_id_of(message: dict) -> int:
    """这条消息在旧格式流水里的整数 id（和 read_all() 的编号保持一致）。"""
    sid, mid = message.get("session_id"), message.get("id")
    for position, item in enumerate(_all_messages(), start=1):
        if item.get("session_id") == sid and item.get("id") == mid:
            return position
    return 0


def read_all() -> list[dict]:
    """旧接口：把各会话消息映射回旧格式，id 为递增整数。"""
    _ensure()
    return [_legacy_view(message, position)
            for position, message in enumerate(_all_messages(), start=1)]


def after(last_id) -> list[dict]:
    try:
        threshold = int(last_id)
    except (TypeError, ValueError):
        threshold = 0
    return [message for message in read_all() if message["id"] > threshold]


def pending() -> list[dict]:
    """旧接口：还没被大脑认领的用户任务（id 大于已处理位置）。"""
    state = _read_state()
    last = int(state.get("last_handled_id") or 0)
    return [
        message for message in read_all()
        if message["id"] > last and message["from"] == "user"
        and message.get("kind") in ("task", "msg")
    ]


def _fmt(message: dict) -> str:
    who = message.get("name") or message.get("from")
    kind = message.get("kind", "msg")
    tag = "" if kind == "msg" else f" [{kind}]"
    return f"#{message['id']:>3} {message['at'][11:19]} {who}{tag}: {message['text']}"


def main() -> int:
    ap = argparse.ArgumentParser(description="群聊消息总线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("post", help="发一条消息")
    p.add_argument("--as", dest="author", required=True, choices=sorted(MEMBERS))
    p.add_argument("--text", required=True)
    p.add_argument("--kind", default="msg")
    p.add_argument("--meta", default=None, help="JSON 字符串")

    p = sub.add_parser("tail", help="看最近消息")
    p.add_argument("-n", type=int, default=20)

    p = sub.add_parser("since", help="看某条之后的消息")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pending", help="看待处理的用户消息")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("ack", help="标记已处理到某条")
    p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("wait", help="阻塞等待新消息")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--timeout", type=int, default=60)

    args = ap.parse_args()

    if args.cmd == "post":
        meta = json.loads(args.meta) if args.meta else None
        msg = post(args.author, args.text, args.kind, meta)
        print(json.dumps(msg, ensure_ascii=False))
        return 0

    if args.cmd == "tail":
        msgs = read_all()
        for message in msgs[-args.n:]:
            print(_fmt(message))
        return 0

    if args.cmd == "since":
        msgs = after(args.after)
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False))
        else:
            for message in msgs:
                print(_fmt(message))
        return 0

    if args.cmd == "pending":
        msgs = pending()
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False))
        else:
            if not msgs:
                print("（没有待处理的用户消息）")
            for message in msgs:
                print(_fmt(message))
        return 0

    if args.cmd == "ack":
        state = _read_state()
        state["last_handled_id"] = max(int(state.get("last_handled_id") or 0), args.id)
        _write_state(state)
        print(f"已标记处理到 #{args.id}")
        return 0

    if args.cmd == "wait":
        deadline = time.time() + args.timeout
        cursor = args.after
        while time.time() < deadline:
            new = after(cursor)
            if new:
                for message in new:
                    print(_fmt(message))
                return 0
            time.sleep(1)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())