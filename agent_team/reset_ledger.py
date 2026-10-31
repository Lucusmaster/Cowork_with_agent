#!/usr/bin/env python3
"""把协作账本清空，做成一份可以发给别人的干净模板。

  python bin/reset_ledger.py --yes     # 清空 room/ 历史与 tasks/ 全部任务卡

只删协作记录，不动 tools/、bin/、config/、docs/。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from .paths import workbench_root  # noqa: E402


ROOT = workbench_root()
ROOM = ROOT / "room"
TASKS = ROOT / "tasks"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def stop_services(port: int) -> None:
    """清账本前先把引擎和网页停掉，否则引擎每 3 秒重建锁，会留下孤儿进程。"""
    try:
        import start_all as launcher  # 同目录
    except ImportError:
        return
    pid = launcher.worker_pid()
    if pid:
        launcher.kill_pid(pid)
        print(f"已先停掉工作流引擎 (pid {pid})")
    for web_pid in launcher.find_web_pid(port):
        launcher.kill_pid(web_pid)
        print(f"已先停掉协作台 (pid {web_pid}, 端口 {port})")


def main() -> int:
    ap = argparse.ArgumentParser(description="清空协作账本（做干净模板用）")
    ap.add_argument("--yes", action="store_true", help="确实要删，才加这个参数")
    ap.add_argument("--port", type=int, default=8899, help="协作台端口（用于停掉它）")
    args = ap.parse_args()

    if not args.yes:
        print("这会删掉 room/ 里的聊天记录和 tasks/ 里的所有任务卡。")
        print("确认无误就加 --yes 再跑一次：python bin/reset_ledger.py --yes")
        return 1

    stop_services(args.port)

    removed_tasks = 0
    if TASKS.is_dir():
        for item in TASKS.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
                removed_tasks += 1
    TASKS.mkdir(parents=True, exist_ok=True)

    if ROOM.is_dir():
        for name in ("messages.jsonl", "state.json", "worker.lock", "worker.log", "web.out",
                     "nudge", "typing.json"):
            (ROOM / name).unlink(missing_ok=True)
        # 会话文件：新账本按会话分文件，漏删会留着聊天历史
        shutil.rmtree(ROOM / "sessions", ignore_errors=True)
        for name in ("planner", "logs", "projects"):
            shutil.rmtree(ROOM / name, ignore_errors=True)

    (ROOM / "messages.jsonl").write_text("", encoding="utf-8")
    (ROOM / "state.json").write_text('{"last_handled_id": 0}', encoding="utf-8")

    print(f"已清空：{removed_tasks} 个任务卡、聊天记录、引擎锁")
    print("现在这份仓库可以直接打包发给别人了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
