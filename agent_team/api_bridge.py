"""OpenAI 兼容 API 桥：把提示词 POST 到 `{base_url}/chat/completions`，返回和 `run_codex` 同形的结果。

只收发文字——改文件、跑命令都不行，所以 API 模型只能做协调 / 规划 / 复核，不能当执行者
（`models.set_model`、preflight、界面三处都拦着）。

和另外两个桥一样：**不抛异常**，失败用 `ok=False` 表达；结果写 `.last.md` / `.out.txt` /
`.err.txt`，下游读的是 `result["last"]` 那个文件。key 只在发请求的那一刻从环境变量读，
不会出现在任何落盘文件、summary 或返回值里（上游报错回显请求头时也要先抹掉）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import agent_clis
from . import api_providers
from . import models

CAPACITY_LINE = "[api] 上游把这次请求判为过载（HTTP {code}）：the selected model is at capacity, retry later"


def _prefix(out_prefix: str | None, attempt: int) -> Path:
    if out_prefix:
        prefix = Path(out_prefix).resolve()
    else:
        prefix = agent_clis.ROOT / "room" / "api-runs" / f"api-{os.getpid()}-{int(time.time())}"
    if attempt > 0:
        prefix = Path(str(prefix) + f"-attempt{attempt}")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    return prefix


def run_api(prompt: str, workspace: str | None = None, out_prefix: str | None = None,
            sandbox: str = "read-only", timeout: int = 300, model: str = "",
            reasoning_effort: str = "", attempt: int = 0) -> dict:
    """跑一次 OpenAI 兼容 API。`sandbox` 只为和另外两个桥保持同形，API 不碰文件。"""
    ws = Path(workspace or agent_clis.ROOT).resolve()
    prefix = _prefix(out_prefix, attempt)
    last_path = Path(str(prefix) + ".last.md")
    out_path = Path(str(prefix) + ".out.txt")
    err_path = Path(str(prefix) + ".err.txt")

    spec = models.registry().get(model) or {}
    provider = str(spec.get("provider") or "").strip()
    remote = str(spec.get("remote_id") or "").strip() or (
        model.split(":", 1)[1] if ":" in model else model)
    entry = api_providers.load().get(provider) if provider else {}
    base_url = str((entry or {}).get("base_url") or spec.get("base_url") or "").rstrip("/")
    env = str((entry or {}).get("api_key_env") or spec.get("api_key_env") or "")

    info = {"workspace": str(ws), "codex": f"api:{provider or '?'}",
            "model": remote or model,
            "reasoning_effort": "（API 适配未使用推理档，已忽略）" if reasoning_effort else "",
            "tokens": None, "scratch_removed": 0, "agent": "api"}

    def fail(code: int, summary: str, text: str = "", *, timedout: bool = False,
             seconds: int = 0) -> dict:
        err_path.write_text(str(text or summary), encoding="utf-8")
        out_path.write_text("", encoding="utf-8")
        return {**info, "ok": False, "exit": code, "timedout": timedout, "seconds": seconds,
                "last": "", "stdout": str(out_path), "stderr": str(err_path), "summary": summary}

    if not provider:
        return fail(2, f"{model} 在注册表里没有 provider，不知道该发到哪个 API")
    if not base_url:
        return fail(2, f"API 提供方 {provider} 没配地址：在换模型菜单里补上")
    key = api_providers.key_for(provider)
    if not key:
        return fail(2, (f"没读到 key（环境变量 {env or '（未填写）'} 为空，本机也没保存过）："
                        "在换模型菜单里贴 key，或设好环境变量后重启协作台"))

    url = base_url + "/chat/completions"
    body = {"model": remote, "messages": [{"role": "user", "content": str(prompt or "")}]}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    except ValueError:                  # 地址被手改坏：当成模型不可用，别让派单抛异常
        return fail(2, f"API 地址不合法：{base_url}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        elapsed = int(time.time() - started)
        try:
            detail = exc.read(65_536).decode("utf-8", "replace")
        except Exception:      # noqa: BLE001 - 错误体读不出来也不能让桥抛异常
            detail = ""
        detail = api_providers._scrub(detail, key)[:2000]
        if exc.code in (429, 503):
            # 这一行是给 fallback.is_capacity_error() 看的：降级链要靠它换模型
            return fail(exc.code, f"API 过载（HTTP {exc.code}）",
                        CAPACITY_LINE.format(code=exc.code) + "\n" + detail,
                        seconds=elapsed)
        if exc.code in (401, 403):
            return fail(exc.code, f"API key 无效或没设置（环境变量 {env}）",
                        f"HTTP {exc.code}\n{detail}", seconds=elapsed)
        return fail(exc.code, f"API 返回 HTTP {exc.code}", detail or f"HTTP {exc.code}",
                    seconds=elapsed)
    except (urllib.error.URLError, TimeoutError) as exc:
        elapsed = int(time.time() - started)
        reason = str(getattr(exc, "reason", exc))
        timedout = isinstance(exc, TimeoutError) or "timed out" in reason.lower()
        summary = (f"API 请求超时（{timeout}s）" if timedout
                   else f"连不上 {base_url}：{api_providers._scrub(reason, key)[:200]}")
        return fail(124 if timedout else 1, summary, api_providers._scrub(reason, key),
                    timedout=timedout, seconds=elapsed)
    except OSError as exc:
        return fail(1, f"API 请求失败：{type(exc).__name__}")
    except ValueError:                  # 地址被手改成不是 URL：当成不可用，别让派单抛异常
        return fail(2, f"API 地址不合法：{base_url}")

    elapsed = int(time.time() - started)
    out_path.write_text(api_providers._scrub(raw, key), encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return fail(2, "API 返回的不是 JSON", raw[:2000], seconds=elapsed)
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return fail(2, "API 返回的不是 OpenAI 兼容的 chat/completions 响应",
                    raw[:2000], seconds=elapsed)

    last_path.write_text(content, encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    tokens = usage.get("total_tokens")
    tokens = int(tokens) if isinstance(tokens, (int, float)) else None
    summary = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
    return {**info, "ok": True, "exit": 0, "timedout": False, "seconds": elapsed,
            "last": str(last_path), "stdout": str(out_path), "stderr": str(err_path),
            "summary": summary, "tokens": tokens}


__all__ = ["run_api"]