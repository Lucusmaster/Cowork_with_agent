#!/usr/bin/env python3
"""群聊消息总线：所有成员（人、DeepSeek、Codex、人工接手）都往同一个房间发消息。

用法：
  python bin/room.py post --as user --text "帮我做 X"        # 发消息
  python bin/room.py post --as codex --text "..." --kind result
  python bin/room.py tail -n 20                              # 看最近消息
  python bin/room.py since --after 12 [--json]               # 看 12 之后的新消息
  python bin/room.py pending                                 # 看还没被大脑认领的用户任务
  python bin/room.py ack --id 12                             # 标记已认领
  python bin/room.py wait --after 12 --timeout 60            # 阻塞等新消息
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOM = ROOT / "room"
MESSAGES = ROOM / "messages.jsonl"
STATE = ROOM / "state.json"

# Windows 控制台默认 GBK，表情符号会炸；统一按 UTF-8 输出（不可编码字符降级处理）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

MEMBERS = {
    "user": "我",
    "deepseek": "DeepSeek（大脑）",
    "codex": "Codex（执行者）",
    "relay": "人工转发",
    "system": "调度",
}


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


def read_all() -> list[dict]:
    _ensure()
    out = []
    for line in MESSAGES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def post(author: str, text: str, kind: str = "msg", meta: dict | None = None) -> dict:
    _ensure()
    msgs = read_all()
    msg = {
        "id": (msgs[-1]["id"] + 1) if msgs else 1,
        "at": datetime.now().isoformat(timespec="seconds"),
        "from": author,
        "name": MEMBERS.get(author, author),
        "kind": kind,
        "text": text,
    }
    if meta:
        msg["meta"] = meta
    with MESSAGES.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        fh.flush()
    return msg


def after(last_id: int) -> list[dict]:
    return [m for m in read_all() if m["id"] > last_id]


def pending() -> list[dict]:
    state = _read_state()
    last = int(state.get("last_handled_id") or 0)
    return [
        m for m in read_all()
        if m["id"] > last and m["from"] == "user" and m.get("kind") in ("task", "msg")
    ]


def _fmt(m: dict) -> str:
    who = m.get("name") or m.get("from")
    kind = m.get("kind", "msg")
    tag = "" if kind == "msg" else f" [{kind}]"
    return f"#{m['id']:>3} {m['at'][11:19]} {who}{tag}: {m['text']}"


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
        for m in msgs[-args.n:]:
            print(_fmt(m))
        return 0

    if args.cmd == "since":
        msgs = after(args.after)
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False))
        else:
            for m in msgs:
                print(_fmt(m))
        return 0

    if args.cmd == "pending":
        msgs = pending()
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False))
        else:
            if not msgs:
                print("（没有待处理的用户消息）")
            for m in msgs:
                print(_fmt(m))
        return 0

    if args.cmd == "ack":
        state = _read_state()
        state["last_handled_id"] = max(int(state.get("last_handled_id") or 0), args.id)
        _write_state(state)
        print(f"已标记处理到 #{args.id}")
        return 0

    if args.cmd == "wait":
        deadline = time.time() + args.timeout
        curs = args.after
        while time.time() < deadline:
            new = after(curs)
            if new:
                for m in new:
                    print(_fmt(m))
                return 0
            time.sleep(1)
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())