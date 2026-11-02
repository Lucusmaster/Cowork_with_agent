"""Claude Code 执行桥：把一份提示词交给 `claude -p` 无头运行，返回和 `run_codex` 同形的结果。

Claude Code 没有 codex 那样的 `--sandbox` 开关，权限用 `--permission-mode` + `--disallowedTools` 表达：

- **只读**（复核/规划）：默认权限模式，并显式禁掉所有写工具和 Bash。无头模式下没人点"批准"，
  需要批准的工具本来就会被拒；再写一遍 deny 是防用户自己的 settings.json 预先放行了 Edit/Bash
  （deny 优先于 allow）。复核者动没动文件，team.py 的 `review_wrote` 还会再对一次。
- **可写**（执行者）：`acceptEdits` 只自动放行文件编辑，Bash 仍然禁掉——要放开命令得用
  `bypassPermissions`，那就没有任何边界了。所以 Claude 执行者**改文件、不跑测试**；
  验收本来就由协作台在沙箱外跑，只认退出码。

提示词走 stdin，命令行上只有固定参数和过了白名单的模型 id，不拼任何用户文本。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import agent_clis
from . import fallback
from .winproc import quiet_flags

READ_ONLY_DENY = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
EXEC_DENY = ("Bash",)
INSTRUCTION = "Follow the task description provided on stdin."


def build_args(exe: str, model: str, sandbox: str) -> list[str]:
    args = [exe, "-p", INSTRUCTION, "--output-format", "json"]
    if model:
        args += ["--model", model]
    if sandbox == "read-only":
        args += ["--permission-mode", "default", "--disallowedTools", *READ_ONLY_DENY]
    else:
        args += ["--permission-mode", "acceptEdits", "--disallowedTools", *EXEC_DENY]
    return args


def parse_output(stdout: str) -> dict:
    """`--output-format json` 的结果对象：取 result 文本、是否出错、tokens。认不出就当纯文本。"""
    raw = str(stdout or "").strip()
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    if not isinstance(data, dict):
        tail = [ln for ln in raw.splitlines() if ln.strip()][-60:]
        return {"text": "\n".join(tail), "is_error": False, "tokens": None, "json": False}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    total = sum(int(usage.get(k) or 0) for k in keys if isinstance(usage.get(k), (int, float)))
    return {"text": str(data.get("result") or ""), "is_error": bool(data.get("is_error")),
            "tokens": total or None, "json": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时只杀自己起的这棵进程树（claude.cmd → node），不按名字杀。"""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=15, creationflags=quiet_flags())
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_claude(prompt: str, workspace: str | None = None, out_prefix: str | None = None,
               sandbox: str = "workspace-write", timeout: int = 1200, model: str = "",
               reasoning_effort: str = "", fallback_models: list[str] | None = None) -> dict:
    """跑一次 Claude Code。不抛异常，失败用 ok=False 表达；字段和 run_codex 一致。

    容量满载（`at capacity`）时按 `fallback_models` 依次降级；重试和判定都在
    `fallback` 里，这里只负责"用某个模型跑一次"。走 `run_codex` 的调用方已经由
    `run_codex` 统一做降级，这里保留同名参数是为了直接调用 `run_claude` 时行为一致。
    """
    models_to_try = fallback.attempt_models(model, fallback_models)

    def attempt(try_model: str, index: int) -> dict:
        return _run_claude_once(
            prompt=prompt,
            workspace=workspace,
            out_prefix=out_prefix,
            sandbox=sandbox,
            timeout=timeout,
            model=try_model,
            reasoning_effort=reasoning_effort,
            attempt=index,
        )

    return fallback.run_with_fallback(attempt, models_to_try, label="claude")


def _run_claude_once(prompt: str, workspace: str | None, out_prefix: str | None,
                     sandbox: str, timeout: int, model: str, reasoning_effort: str,
                     attempt: int) -> dict:
    """执行一次 Claude Code 调用（内部函数，供 run_claude / run_codex 的降级重试使用）。"""
    ws = Path(workspace or agent_clis.ROOT).resolve()
    if out_prefix:
        prefix = Path(out_prefix).resolve()
        if attempt > 0:
            prefix = Path(str(prefix) + f"-attempt{attempt}")
    else:
        prefix = agent_clis.ROOT / "room" / "claude-runs" / f"claude-{os.getpid()}-{int(time.time())}"
    prefix.parent.mkdir(parents=True, exist_ok=True)
    last_path = Path(str(prefix) + ".last.md")
    out_path = Path(str(prefix) + ".out.txt")
    err_path = Path(str(prefix) + ".err.txt")
    exe, _ = agent_clis.resolve_path("claude")
    base = {"workspace": str(ws), "codex": exe, "model": model or "（Claude 默认）",
            "reasoning_effort": "（Claude CLI 没有推理档，已忽略）" if reasoning_effort else "",
            "tokens": None, "scratch_removed": 0, "agent": "claude"}

    def fail(code: int, summary: str, stderr: str = "") -> dict:
        err_path.write_text(stderr or summary, encoding="utf-8")
        return {**base, "ok": False, "exit": code, "timedout": False, "seconds": 0,
                "last": "", "stdout": "", "stderr": str(err_path), "summary": summary}

    if not exe:
        return fail(127, "找不到 claude：在换模型菜单里点 Claude Code，检测或粘贴它的路径")
    if model and not agent_clis.MODEL_ID.match(model):
        return fail(2, f"模型 id 含不允许的字符：{model!r}")

    started = time.time()
    timedout = False
    try:
        proc = subprocess.Popen(build_args(exe, model, sandbox), cwd=str(ws),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=quiet_flags())
    except OSError as exc:
        return fail(127, f"启动 Claude Code 失败：{exc}")
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timedout = True
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            stdout, stderr = "", ""
        rc = 124
        stderr = (stderr or "") + f"\n[bridge] 超时 {timeout}s，已终止。"
    elapsed = int(time.time() - started)

    out_path.write_text(stdout or "", encoding="utf-8")
    err_path.write_text(stderr or "", encoding="utf-8")
    parsed = parse_output(stdout or "")
    last_path.write_text(parsed["text"], encoding="utf-8")
    ok = rc == 0 and not timedout and not parsed["is_error"]
    summary = next((ln.strip() for ln in parsed["text"].splitlines() if ln.strip()), "")
    if parsed["is_error"]:
        summary = "Claude Code 报错：" + (summary or "（没有说明）")
    return {**base, "ok": ok, "exit": rc, "timedout": timedout, "seconds": elapsed,
            "last": str(last_path), "stdout": str(out_path), "stderr": str(err_path),
            "summary": summary, "tokens": parsed["tokens"]}
