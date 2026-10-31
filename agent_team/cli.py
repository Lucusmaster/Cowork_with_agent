"""协作台统一入口：agent-team <命令>。

真正干活的代码在各自模块里，这里只做分发，方便 `pip install -e .` 之后有个正经命令。
"""
from __future__ import annotations

import argparse
import sys

from . import __version__


def _run(module: str, argv: list[str]) -> int:
    """把子命令转发给对应模块的 main()，参数原样透传。"""
    import importlib

    mod = importlib.import_module(f"agent_team.{module}")
    saved = sys.argv
    sys.argv = [f"agent_team.{module}"] + argv
    try:
        return int(mod.main() or 0)
    finally:
        sys.argv = saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-team",
        description="DeepSeek 规划、Codex 执行、只读复核的群聊式多 Agent 协作台",
    )
    parser.add_argument("--version", action="version", version=f"agent-team {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("start", help="启动协作台网页 + 引擎")
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--restart", action="store_true", help="先停再起")

    p = sub.add_parser("stop", help="停掉协作台与引擎")
    p.add_argument("--port", type=int, default=8899)

    sub.add_parser("status", help="看服务与任务状态")
    sub.add_parser("doctor", help="体检：环境、端口、配置")
    sub.add_parser("reset", help="清空 room/ 与 tasks/（要 --yes）")

    p = sub.add_parser("task", help="命令行交一条任务（自动启停）")
    p.add_argument("--text", required=True)
    p.add_argument("--port", type=int, default=8899)

    p = sub.add_parser("say", help="往群里发一条消息")
    p.add_argument("--as", dest="author", default="deepseek")
    p.add_argument("--text", required=True)
    p.add_argument("--kind", default="msg")

    p = sub.add_parser("tail", help="看群里最近的消息")
    p.add_argument("-n", type=int, default=20)

    p = sub.add_parser("wait", help="等某张任务卡到终态")
    p.add_argument("-t", "--task", required=True)
    p.add_argument("--timeout", type=int, default=1800)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "start":
        extra = ["--restart"] if args.restart else []
        return _run("start_all", ["--port", str(args.port), *extra])
    if args.command == "stop":
        return _run("start_all", ["--port", str(args.port), "--stop"])
    if args.command == "status":
        return _run("start_all", ["--status"])
    if args.command == "doctor":
        return _run("doctor", [])
    if args.command == "reset":
        return _run("reset_ledger", [])
    if args.command == "task":
        return _run("run_task", ["--text", args.text, "--port", str(args.port)])
    if args.command == "say":
        return _run("room", ["post", "--as", args.author, "--text", args.text, "--kind", args.kind])
    if args.command == "tail":
        return _run("room", ["tail", "-n", str(args.n)])
    if args.command == "wait":
        return _run("await_task", ["-t", args.task, "--timeout", str(args.timeout)])

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())