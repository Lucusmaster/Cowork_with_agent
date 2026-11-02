"""执行期间的心跳：让"正在跑"这件事在群里看得见。

以前派发出去到跑完那 2–4 分钟里，群里只有一条"开始第 N 轮执行"，用户会以为卡死
了（这正是"写完 plan.md 就停了"的观感来源）。这里起一个后台线程，每隔一段时间
把"已跑多久 + 日志最新一行"发成一条进度消息，长任务也能看到它还在动。

只做两件事：读日志尾巴、按间隔发一条；不参与业务判断，出错一律吞掉。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


def tail_line(path: str | Path, limit: int = 120) -> str:
    """取文件最后一条有内容的行（日志可能正在被写，读失败就当没有）。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def newest_file(directory: str | Path, pattern: str) -> Path | None:
    """按修改时间取最新匹配的文件（降级重试会生成 exec-02-attempt1.err.txt 这类）。"""
    try:
        files = [p for p in Path(directory).glob(pattern) if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    try:
        return max(files, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def human_seconds(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


class Beater:
    """在 `with` 里跑：后台每 interval 秒发一条"已 Xs，最新：…"。"""

    def __init__(self, label: str, *, tid: str = "", directory: str | Path | None = None,
                 pattern: str = "", interval: float = 30.0, emit=None) -> None:
        self.label = label
        self.tid = tid
        self.directory = directory
        self.pattern = pattern
        self.interval = max(0.05, float(interval))
        self.emit = emit
        self.beats = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0

    # --- 内部 ---
    def _latest(self) -> str:
        if not self.directory or not self.pattern:
            return ""
        path = newest_file(self.directory, self.pattern)
        return tail_line(path) if path else ""

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.beats += 1
            elapsed = human_seconds(time.time() - self._started)
            line = self._latest()
            text = f"{self.label}：已跑 {elapsed}"
            if line:
                text += f"，最新：{line}"
            self._send(text)

    def _send(self, text: str) -> None:
        if self.emit is None:
            return
        try:
            self.emit(text)
        except Exception:  # noqa: BLE001 - 心跳发不出去不影响正事
            pass

    # --- 生命周期 ---
    def start(self) -> "Beater":
        self._started = time.time()
        self._thread = threading.Thread(target=self._loop, name="progress-beat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> "Beater":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()