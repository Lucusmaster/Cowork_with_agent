#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Codex 执行者桥：把一份提示词交给 `codex exec` 无头运行，产物落盘。

只做一件事：可靠启动 Codex、处理超时、拿到最终消息和完整日志。
任务协议（拆解、轮次、验收）在 bin/team.py，不在这里。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from .winproc import quiet_flags
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

# Codex 的会话日志、临时文件很大（本机实测 session 已超 1GB），
# 默认落到 C:\Users\<user>\.codex。这里把「本次工作区」的 CODEX_HOME
# 指进工作区自己的 .codex-home/，只从用户目录复制登录凭据，不动历史会话。
CREDENTIAL_FILES = ("auth.json", "config.toml")


def codex_home(workspace: Path) -> Path:
    """本次运行用的 CODEX_HOME。

    放在工具仓库根目录下的 `.codex-home/`，而不是用户主目录；
    也不按项目各放一份——Codex 会在 home 里放插件缓存和 sandbox 二进制
    （实测单个 home 可达 450MB），每个项目复制一份太浪费。
    """
    override = os.environ.get("CODEX_HOME", "").strip() or os.environ.get("AGENT_CODEX_HOME", "").strip()
    home = Path(override) if override else (ROOT / ".codex-home")
    home.mkdir(parents=True, exist_ok=True)
    source = Path.home() / ".codex"
    for name in CREDENTIAL_FILES:
        src, dst = source / name, home / name
        if not src.is_file():
            continue
        if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime:
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    return home


def find_codex() -> str:
    """按优先级定位 codex 可执行文件：环境变量 → 官方安装目录（Windows）→ PATH。"""
    candidates: list[str] = []
    env = os.environ.get("CODEX_EXE", "").strip()
    if env and Path(env).is_file():
        candidates.append(env)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        if base.is_dir():
            exes = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            candidates.extend(str(p) for p in exes)
    which = shutil.which("codex")
    if which:
        candidates.append(which)
    if not candidates:
        raise SystemExit("找不到 codex：请设置环境变量 CODEX_EXE，或安装 Codex CLI（codex 命令需在 PATH 里）。")
    return candidates[0]


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _session_tokens(started_ts: float, home: Path | None = None) -> int | None:
    """读本次调用对应的 Codex 会话日志，返回该会话累计 tokens；读不到返回 None。"""
    try:
        bases = []
        if home is not None:
            bases.append(home / "sessions")
        bases.append(Path(os.environ.get("USERPROFILE", "")) / ".codex" / "sessions")
        base = next((b for b in bases if b.is_dir()), None)
        if base is None:
            return None
        today = datetime.now()
        day_dir = base / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}"
        files = sorted(day_dir.glob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime) if day_dir.is_dir() else []
        files = [p for p in files if p.stat().st_mtime >= started_ts - 3]
        if not files:
            return None
        total = None
        for line in files[-1].read_text(encoding="utf-8", errors="replace").splitlines():
            if '"token_count"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") or {}
            usage = (payload.get("info") or {}).get("total_token_usage") or payload.get("total_token_usage")
            if usage and usage.get("total_tokens"):
                total = int(usage["total_tokens"])
        return total
    except OSError:
        return None


def run_codex(
    prompt: str,
    workspace: str | None = None,
    out_prefix: str | None = None,
    sandbox: str = "workspace-write",
    timeout: int = 1200,
    model: str = "",
    reasoning_effort: str = "",
) -> dict:
    """跑一次 Codex，返回结果字典（不抛异常，失败用 ok=False 表达）。"""
    ws = Path(workspace or ROOT).resolve()
    scratch = ws / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    if out_prefix:
        prefix = Path(out_prefix).resolve()
    else:
        prefix = scratch / f"codex-{os.getpid()}-{int(time.time())}"
    prefix.parent.mkdir(parents=True, exist_ok=True)

    codex = find_codex()
    last_path = Path(str(prefix) + ".last.md")
    out_path = Path(str(prefix) + ".out.txt")
    err_path = Path(str(prefix) + ".err.txt")

    args = [
        codex, "exec",
        "--skip-git-repo-check",
        "--cd", str(ws),
        "--sandbox", sandbox,
        "--output-last-message", str(last_path),
        "--color", "never",
    ]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["-c", f"model_reasoning_effort={reasoning_effort}"]
    args.append("-")  # 从 stdin 读取提示词，避开命令行长度与转义问题

    # Codex 的 workspace-write 沙箱只允许写工作区；把临时目录指进工作区，
    # 执行者才能真的跑 tempfile 类测试（否则测试会因 PermissionError 假失败）。
    # CODEX_HOME 同样落进工作区，会话日志不再堆到 C 盘用户目录。
    home = codex_home(ws)
    child_env = dict(os.environ)
    child_env.update({
        "TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch),
        "CODEX_HOME": str(home),
    })

    started = time.time()
    timedout = False
    try:
        proc = subprocess.run(
            args,
            cwd=str(ws),
            input=prompt,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env, creationflags=quiet_flags())
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timedout = True
        rc = 124
        stdout, stderr = _as_text(exc.stdout), _as_text(exc.stderr)
        stderr += f"\n[bridge] 超时 {timeout}s，已终止。"
    except OSError as exc:
        return {
            "ok": False, "exit": 127, "timedout": False, "seconds": 0,
            "codex": codex, "workspace": str(ws), "last": "", "stdout": "", "stderr": "",
            "summary": f"启动 Codex 失败：{exc}",
        }
    elapsed = int(time.time() - started)

    out_path.write_text(stdout or "", encoding="utf-8")
    err_path.write_text(stderr or "", encoding="utf-8")

    last = last_path.read_text(encoding="utf-8") if last_path.is_file() else ""
    if not last.strip():
        tail = [ln for ln in (stdout or "").splitlines() if ln.strip()][-60:]
        last = "\n".join(tail)
        last_path.write_text(last, encoding="utf-8")

    summary = next((ln.strip() for ln in last.splitlines() if ln.strip()), "")
    return {
        "ok": rc == 0 and not timedout,
        "exit": rc,
        "timedout": timedout,
        "seconds": elapsed,
        "codex": codex,
        "workspace": str(ws),
        "last": str(last_path),
        "stdout": str(out_path),
        "stderr": str(err_path),
        "summary": summary,
        "model": model or "（Codex 默认）",
        "reasoning_effort": reasoning_effort or "（模型默认）",
        "tokens": _session_tokens(started, home),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Codex 执行者桥")
    ap.add_argument("--prompt-file", required=True, help="UTF-8 提示词文件")
    ap.add_argument("--workspace", default=str(ROOT))
    ap.add_argument("--out-prefix", default="")
    ap.add_argument("--sandbox", default="workspace-write",
                    choices=["read-only", "workspace-write", "danger-full-access"])
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--model", default="")
    ap.add_argument("--reasoning-effort", default="")
    args = ap.parse_args()

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    result = run_codex(
        prompt,
        workspace=args.workspace,
        out_prefix=args.out_prefix or None,
        sandbox=args.sandbox,
        timeout=args.timeout,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())