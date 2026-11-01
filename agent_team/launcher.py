"""桌面双击启动：确保协作台在跑，然后用**一次性链接**打开浏览器。

为什么不直接套 `cli start`：那不是双击场景。

- **已经在跑**：不该重启（会打断正在执行的任务），只把页面打开就行；
- **没在跑**：起服务 → 等到 `GET /` 真的返回 200 再开浏览器（不是睡固定几秒赌）；
- **链接**：必须是 `?k=` 一次性码。0.9.12 起长期令牌禁止进 URL（会留在历史和 Referer），
  所以直接开 `http://127.0.0.1:8899` 只会看到解锁卡片，而不是界面。

这段逻辑放在 Python 里而不是 .bat 里，是因为 cmd 的引号/编码/错误码太容易写错，
而且写成函数才能被测试覆盖。
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from .paths import workbench_root

for _stream in (sys.stdout, sys.stderr):      # 双击时输出走管道/控制台，中文别乱码
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = workbench_root()
CODE_FILE = ROOT / "room" / "web-code"
DEFAULT_PORT = 8899
READY_TIMEOUT = 30.0          # start_all 起服务一般 1-2 秒；给足余量但不无限等


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def page_ready(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    """页面真能打开吗：`GET /` 返回 200 才算（它不需要令牌，界面的解锁卡片也在里面）。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout) as resp:
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def one_time_url(port: int, host: str = "127.0.0.1") -> str:
    """带一次性引导码的地址；读不到码就退回裸地址（页面会要求粘贴令牌）。"""
    base = f"http://{host}:{port}/"
    try:
        code = CODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        code = ""
    return f"{base}?k={code}" if code else base


def wait_ready(port: int, timeout: float = READY_TIMEOUT, host: str = "127.0.0.1") -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if page_ready(port, host):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def start_server(port: int) -> int:
    """后台起协作台（start_all 自己会把服务/引擎摘成脱离进程）。"""
    from .cli import _run

    return _run("start_all", ["--port", str(port)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agent-team.launcher",
        description="双击用的启动器：没在跑就起，起来了就直接开页面（一次性链接）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="只起服务，不打开浏览器")
    ap.add_argument("--timeout", type=float, default=READY_TIMEOUT)
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    port = args.port
    if page_ready(port):
        print(f"协作台已经在跑（端口 {port}），直接打开页面。")
    else:
        if port_open(port):
            print(f"端口 {port} 被别的程序占着，但页面打不开。换个端口：--port 8900", flush=True)
            return 1
        print(f"正在启动协作台（端口 {port}）…", flush=True)
        code = start_server(port)
        if code != 0:
            print(f"启动命令失败（退出码 {code}），看看 room/worker.log", flush=True)
            return 1
        if not wait_ready(port, args.timeout):
            print(f"等了 {args.timeout:.0f} 秒页面还没就绪，看看 room/worker.log", flush=True)
            return 1
        print("协作台已就绪。")

    url = one_time_url(port)
    if "?k=" not in url:
        print("找不到一次性引导码（room/web-code），打开的是解锁页；"
              "令牌可以到 room/web-token 里抄。")
    print(f"地址：{url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:      # noqa: BLE001 - 打不开浏览器不算启动失败
            print(f"（没能自动打开浏览器：{exc}，手动点上面的地址）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())