#!/usr/bin/env python3
"""环境自检：换一台机器、复制一份仓库之后，先跑这个再发任务。

  python bin/doctor.py [--port 8899]
"""
from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .resources import resource
from .winproc import quiet_flags

ROOT = Path(__file__).resolve().parent.parent

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

OK, WARN, BAD = "✓", "!", "✗"
rows: list[tuple[str, str, str]] = []


def add(level: str, name: str, detail: str) -> None:
    rows.append((level, name, detail))


def check_python() -> None:
    version = sys.version_info
    text = f"{platform.python_implementation()} {version.major}.{version.minor}.{version.micro} ({platform.system()})"
    if version >= (3, 10):
        add(OK, "Python", text)
    else:
        add(BAD, "Python", text + " —— 需要 3.10 及以上")


def check_git() -> None:
    git = shutil.which("git")
    if not git:
        add(BAD, "git", "没找到 git，任务卡、差异、复核都依赖它")
        return
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(ROOT),
                          capture_output=True, text=True, errors="replace", creationflags=quiet_flags())
    if proc.returncode != 0:
        add(WARN, "git", f"{git}（本目录还不是 git 仓库，建议 git init 以便留痕）")
    else:
        add(OK, "git", proc.stdout.strip())


def check_codex() -> None:
    try:
        from .codex_bridge import find_codex
        exe = find_codex()
    except (SystemExit, Exception) as exc:  # noqa: BLE001
        add(BAD, "Codex CLI", f"定位失败：{exc}")
        return
    add(OK, "Codex CLI", exe)
    auth = Path.home() / ".codex" / "auth.json"
    if auth.is_file():
        add(OK, "Codex 登录态", str(auth))
    else:
        add(WARN, "Codex 登录态",
            f"{auth} 不存在；先在终端跑一次 `codex login`（本机用 ChatGPT 账号登录）")


def check_config() -> None:
    path = resource("config", "team.json")
    if not path.is_file():
        add(BAD, "模型配置", f"缺 {path}")
        return
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        add(BAD, "模型配置", f"解析失败：{exc}")
        return
    executor = (cfg.get("executor") or {}).get("model", "?")
    reviewer = (cfg.get("reviewer") or {}).get("model", "?")
    add(OK, "模型配置", f"执行 {executor} / 复核 {reviewer}（在 config/team.json 里改）")


def check_layout() -> None:
    required = ["agent_team/team.py", "agent_team/worker.py", "agent_team/room.py",
                "agent_team/room_web.py", "agent_team/codex_bridge.py",
                "config/team.json", "agents/reviewer.md"]
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        add(BAD, "仓库结构", "缺文件：" + "、".join(missing))
        return
    add(OK, "仓库结构", f"完整（根目录 {ROOT}）")
    try:
        probe = ROOT / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(OK, "写权限", str(ROOT))
    except OSError as exc:
        add(BAD, "写权限", f"写不了 {ROOT}：{exc}")


def check_port(port: int) -> None:
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state?after=999999", timeout=2) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        busy = False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            busy = sock.connect_ex(("127.0.0.1", port)) == 0
        add(WARN if not busy else BAD, f"协作台端口 {port}",
            "空闲（还没启动，正常）" if not busy else "被别的程序占着，换一个 --port")
        return

    served = Path(str(payload.get("root") or "")).resolve()
    if served == ROOT.resolve():
        add(OK, f"协作台端口 {port}", "在跑，而且就是这个仓库的实例")
    else:
        add(BAD, f"协作台端口 {port}", f"被另一个仓库的实例占着（{served}），换端口或用那个仓库")


def main() -> int:
    port = 8899
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            pass

    check_python()
    check_git()
    check_layout()
    check_codex()
    check_config()
    check_port(port)

    width = max(len(name) for _, name, _ in rows)
    print("多 Agent 协作台 · 环境自检\n")
    for level, name, detail in rows:
        print(f"  {level} {name.ljust(width)}  {detail}")

    bad = [r for r in rows if r[0] == BAD]
    warn = [r for r in rows if r[0] == WARN]
    print()
    if bad:
        print(f"有 {len(bad)} 项必须先解决，否则流水线跑不起来。")
        return 1
    if warn:
        print(f"有 {len(warn)} 项提醒，不阻塞。")
    print("可以开工了：")
    print("  Windows : agent-team start   （或 powershell -ExecutionPolicy Bypass -File bin\\start_all.ps1）")
    print("  macOS/Linux : agent-team start   （或 bash bin/start_all.sh）")
    print(f"  然后打开 http://127.0.0.1:{port}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())