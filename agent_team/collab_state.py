#!/usr/bin/env python3
"""协作状态查询：给"会话里提示协作已结束"用。

  python bin/collab_state.py status   # 打印当前状态
  python bin/collab_state.py check    # 只有「刚结束、还没通知过」才输出 ENDED|时间|原因
  python bin/collab_state.py ack      # 标记这条结束已通知

`check` 是幂等的：同一个结束事件只会输出一次，避免会话被反复打扰。
"""
from __future__ import annotations

import json
import sys
from .paths import workbench_root  # noqa: E402

ROOT = workbench_root()
ROOM = ROOT / "room"
STATE = ROOM / "session_state.json"
NOTIFIED = ROOM / "session_state.notified"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_notified() -> str:
    try:
        return NOTIFIED.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    state = read_state()

    if cmd == "status":
        if not state:
            print("没有协作状态记录（协作台还没启动过）")
            return 1
        print(f"协作: {state.get('collab')}  时间: {state.get('at')}  原因: {state.get('reason') or '-'}")
        print(f"仓库: {state.get('root')}")
        return 0

    if cmd == "check":
        if state.get("collab") != "ended":
            return 0
        if read_notified() == state.get("at"):
            return 0            # 这条已经通知过
        print(f"ENDED|{state.get('at')}|{state.get('reason') or '未说明'}")
        return 0

    if cmd == "ack":
        ROOM.mkdir(parents=True, exist_ok=True)
        NOTIFIED.write_text(str(state.get("at") or ""), encoding="utf-8")
        print("已标记：这次结束不再重复提示")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
