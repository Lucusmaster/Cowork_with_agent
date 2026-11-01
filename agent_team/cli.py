"""协作台统一入口：agent-team <命令>。

真正干活的代码在各自模块里，这里只做分发，方便 `pip install -e .` 之后有个正经命令。
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .winproc import quiet_flags

for _stream in (sys.stdout, sys.stderr):      # 中文输出在 Windows 控制台/管道里都别乱码
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


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


def _repo_root():
    """仓库根目录：cli.py 在 ``<root>/agent_team/`` 里，上一级就是根。"""
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def _install_desktop(workspace: str, force: bool, desktop: str = "") -> int:
    """在桌面创建「agent-team」快捷方式。

    真正动手的是 ``bin/install-shortcut.ps1``（WScript.Shell COM 是 Windows 上最稳的做法，
    也不需要额外的 pywin32 依赖）。这里只做三件事：校验工作区、找到脚本、把参数传进去。
    """
    import subprocess
    from pathlib import Path

    root = _repo_root()
    target_ws = Path(workspace).resolve() if workspace else root
    if not (target_ws / ".git").exists():
        print(f"❌ 不是 git 仓库（没有 .git）：{target_ws}")
        print("   协作台要在一个 git 项目里跑，先 cd 到项目根目录，或加 --workspace <路径>")
        return 1

    launcher = root / "bin" / "agent-team-launcher.bat"
    script = root / "bin" / "install-shortcut.ps1"
    if not launcher.is_file() or not script.is_file():
        print("❌ 找不到启动器：")
        print(f"   {launcher}（{'有' if launcher.is_file() else '缺'}）")
        print(f"   {script}（{'有' if script.is_file() else '缺'}）")
        return 1

    if sys.platform != "win32":
        print("❌ 桌面快捷方式只支持 Windows。其他系统直接跑 python -m agent_team.launcher")
        return 1

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
           "-ProjectPath", str(target_ws)]
    if desktop:
        cmd += ["-Desktop", str(Path(desktop).resolve())]
    if force:
        cmd.append("-Force")

    print(f"工作区：{target_ws}")
    print(f"启动器：{launcher}")
    try:
        result = subprocess.run(cmd, check=False, creationflags=quiet_flags())
    except OSError as exc:
        print(f"❌ 调不起 PowerShell：{exc}")
        print(f"   手动执行：{script}")
        return 1
    if result.returncode == 2:
        print("（桌面上已经有快捷方式了，加 --force 覆盖）")
    return 0 if result.returncode in (0, 2) else result.returncode


def _model(action: str, role: str, model: str) -> int:
    """`agent-team model list|set|reset`：改的是运行时覆盖，不动配置文件。"""
    from . import models

    if action in ("set", "reset"):
        if not role:
            print("要改哪个角色？例如：agent-team model set 执行 claude")
            return 2
        target = "" if action == "reset" else model
        ok, message = models.set_model(role, target)
        print(message)
        return 0 if ok else 2

    snap = models.snapshot()
    print("角色      当前模型            来源   可切")
    for row in snap["roles"]:
        print(f"{row['name']:4}  {row['model'] or '（默认）':18}  {row['source']:4}  "
              f"{'是' if row['switchable'] else '否'}")
    print()
    print("已注册模型：")
    for item in snap["registry"]:
        ready = "" if item["ready"] else f"  ← 没装 {item['cli']}"
        aliases = f"  别名: {', '.join(item['aliases'])}" if item["aliases"] else ""
        print(f"  {item['id']:22} {item['cli']:9}{aliases}{ready}")
    if snap["overrides"]:
        print()
        print("临时覆盖（room/model_overrides.json）：", snap["overrides"])
    print()
    print("用法：agent-team model set 执行 claude   /   agent-team model reset 执行")
    return 0


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
    p = sub.add_parser("reset", help="清空 room/ 与 tasks/（要 --yes）")
    p.add_argument("--yes", action="store_true", help="确实要删")
    p.add_argument("--port", type=int, default=8899)

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

    p = sub.add_parser("model", help="看/切换各角色的模型（下一张卡生效）")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "set", "reset"], help="默认 list")
    p.add_argument("role", nargs="?", default="", help="角色：执行/复核/规划/验收")
    p.add_argument("model", nargs="?", default="", help="模型 id 或别名（如 claude、luna）")

    p = sub.add_parser("install-desktop", help="在 Windows 桌面创建启动快捷方式")
    p.add_argument("--workspace", default="", help="工作区路径（不指定就用仓库根目录）")
    p.add_argument("--desktop", default="", help="写到别的桌面目录（测试用）")
    p.add_argument("--force", action="store_true", help="覆盖已存在的快捷方式")

    p = sub.add_parser("mcp", help="MCP 工具服务（stdio），给 Claude Code / Codex / 豆包等调用")
    p.add_argument("--action", choices=["serve", "list", "call"], default="serve")
    p.add_argument("--tool", default="", help="action=call 时指定工具")
    p.add_argument("--args", default="{}", help="action=call 时的 JSON 参数")

    p = sub.add_parser("install-skills",
                       help="把使用说明（skill）装到 DSH / Codex / Claude Code 的 skills 目录")
    p.add_argument("--targets", default="", help="逗号分隔：dsh,codex,claude（默认全装）")
    p.add_argument("--force", action="store_true", help="已有文件直接覆盖，不备份")
    p.add_argument("--dry-run", action="store_true", help="只说会做什么，不动文件")
    p.add_argument("--mcp", action="store_true", help="只打印 MCP 配置片段")
    p.add_argument("--where", action="store_true", help="只看会装到哪儿")

    sub.add_parser("onboard", help="新装之后该做哪几步（装 skill、MCP、桌面图标）")

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
        return _run("reset_ledger", ["--yes"] if args.yes else [])
    if args.command == "task":
        return _run("run_task", ["--text", args.text, "--port", str(args.port)])
    if args.command == "say":
        return _run("room", ["post", "--as", args.author, "--text", args.text, "--kind", args.kind])
    if args.command == "tail":
        return _run("room", ["tail", "-n", str(args.n)])
    if args.command == "wait":
        return _run("await_task", ["-t", args.task, "--timeout", str(args.timeout)])
    if args.command == "model":
        return _model(args.action, args.role, args.model)
    if args.command == "install-desktop":
        return _install_desktop(args.workspace, args.force, args.desktop)
    if args.command == "mcp":
        return _run("mcp_server", ["--action", args.action, "--tool", args.tool,
                                   "--args", args.args])
    if args.command == "install-skills":
        from . import skills_install

        argv_skills: list[str] = []
        if args.targets:
            argv_skills += ["--targets", args.targets]
        if args.force:
            argv_skills.append("--force")
        if args.dry_run:
            argv_skills.append("--dry-run")
        if args.mcp:
            argv_skills.append("--mcp")
        if args.where:
            argv_skills.append("--where")
        return skills_install.main(argv_skills)
    if args.command == "onboard":
        from . import skills_install

        print(skills_install.onboard())
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())