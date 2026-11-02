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
import sys
import time
from datetime import datetime
from . import fallback
from .winproc import run_capped
from pathlib import Path
from .paths import workbench_root  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = workbench_root()

# Codex 的会话日志、临时文件很大（本机实测 session 已超 1GB），
# 默认落到 C:\Users\<user>\.codex。这里把「本次工作区」的 CODEX_HOME
# 指进工作区自己的 .codex-home/，只从用户目录复制登录凭据，不动历史会话。
CREDENTIAL_FILES = ("auth.json", "config.toml")

# 模型端点覆盖：某些型号直连不通（OpenAI 在本机被墙、或只能走自建中转）时，
# 由 config/team.json 的 model_providers 给这个型号单独指定 base_url。
# 用独立的 provider id，避免和用户 config.toml 里已有的 custom 互相覆盖。
PROVIDER_ID = "workbench"
# Codex 0.166 起只认 responses；wire_api="chat" 会直接报错退出。
DEFAULT_WIRE_API = "responses"

# 执行者压根没连上模型端点时的特征。只认 Codex 自己的网络报错措辞，
# 不要写宽（"error"/"timeout" 这种）——否则任务真的失败也会被当成网络问题而不返工。
NETFAIL_MARKERS = (
    "workspace routing discovery timed out",
    "failed to refresh available models",
    "error sending request for url",
    "transport channel closed",
    "failed to connect",
    "connection refused",
)


def _provider_spec(model: str) -> dict:
    """这个型号该用哪个端点（没有覆盖就返回空）。"""
    if not model:
        return {}
    try:
        from . import models
        spec = models.model_provider(model)
    except Exception:  # noqa: BLE001 - 配置读不出来就按默认端点走
        return {}
    return spec if isinstance(spec, dict) else {}


def _provider_key(spec: dict, env_key: str) -> str:
    """密钥：进程环境变量优先，其次 API 菜单里存过的那份（room/api_keys.json）。"""
    from_env = os.environ.get(env_key, "").strip()
    if from_env:
        return from_env
    store = str(spec.get("key_store") or "").strip()
    if not store:
        return ""
    try:
        from . import api_providers
        return api_providers.key_for(store)
    except Exception:  # noqa: BLE001
        return ""


def provider_overrides(model: str) -> tuple[list[str], dict[str, str]]:
    """把 model_providers 里的端点翻成 codex 的 `-c` 覆盖和子进程环境里的密钥。"""
    spec = _provider_spec(model)
    if not spec:
        return [], {}
    base = str(spec.get("base_url") or "").strip().rstrip("/")
    if not base:
        return [], {}
    name = str(spec.get("name") or "自定义中转").strip() or "自定义中转"
    wire = str(spec.get("wire_api") or DEFAULT_WIRE_API).strip() or DEFAULT_WIRE_API
    env_key = str(spec.get("env_key") or spec.get("api_key_env") or "AGENT_PROVIDER_KEY").strip()
    args = [
        "-c", f"model_provider={PROVIDER_ID}",
        "-c", f'model_providers.{PROVIDER_ID}.name="{name}"',
        "-c", f'model_providers.{PROVIDER_ID}.base_url="{base}"',
        "-c", f'model_providers.{PROVIDER_ID}.wire_api="{wire}"',
        "-c", f"model_providers.{PROVIDER_ID}.env_key={env_key}",
    ]
    env: dict[str, str] = {}
    key = _provider_key(spec, env_key)
    if key:
        env[env_key] = key
    return args, env


def classify_env_failure(stderr: str = "", stdout: str = "", summary: str = "",
                         timedout: bool = False, endpoint: str = "") -> str:
    """执行者是不是"根本没连上模型端点"？是就返回一段人话，否则返回空串。

    这种失败不返工、不换模型——换一个型号还是同一个网络，只会再空烧几分钟。
    调用方拿到非空结果就该停手，把原因和修法告诉用户。

    提示要写到"用户照着就能做"的程度：这类失败九成是本机网络（没开 VPN/代理，
    或者代理只接管了浏览器、没接管命令行），所以先让他查网络，再给两条替代路：
    换个本机可达的模型，或者给这个型号配一条中转端点。

    注意：调用方必须只在**模型一个字都没产出**时问这里。Codex 收尾时会打一条
    `error sending request for url (https://chatgpt.com/backend-api/ps/mcp)`
    （它自带的 MCP 通道），那条跟模型端点无关，不能据此判定"连不上"。
    """
    if timedout:
        return ""
    text = "\n".join([str(stderr or ""), str(stdout or ""), str(summary or "")]).lower()
    hit = next((m for m in NETFAIL_MARKERS if m in text), "")
    if not hit:
        return ""
    where = str(endpoint or "").strip() or "OpenAI 默认端点（chatgpt.com / api.openai.com）"
    return (
        f"执行者连不上模型端点（{hit}），目标是 {where}。\n"
        f"这通常是网络问题：这台机器到该端点不通——没开 VPN/代理，或者代理只接管了浏览器、"
        f"没接管命令行里的 codex。\n"
        f"三条处理办法，任选一条：\n"
        f"1) 连上 VPN/代理后，把这张卡重投一次（不用改任何配置）；\n"
        f"2) 换个本机可达的模型：网页右栏点「执行」的模型徽章，或在群里发 "
        f"`/model 执行 <型号>`；\n"
        f"3) 用的是自建中转的话，在 room/model_providers.json 里给这个型号配 base_url"
        f"（写法见 README「执行者端点不通」一节）。"
    )


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
    try:                                   # 换模型菜单里保存过的路径（room/agent_clis.json）
        from . import agent_clis
        saved, source = agent_clis.resolve_path("codex")
        if saved and source == "已保存":
            candidates.append(saved)
    except Exception:  # noqa: BLE001 - 登记文件坏了就按老办法找
        pass
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


def _route(model: str) -> str:
    """模型 → 用哪个 CLI 跑。没写 cli、或者注册表读不了，都按 codex（老配置零改动）。"""
    if not model:
        return "codex"
    try:
        from . import models
        spec = models.registry().get(model) or {}
    except Exception:  # noqa: BLE001
        return "codex"
    return str(spec.get("cli") or "codex").strip() or "codex"


def _no_adapter(cli: str, model: str, workspace: str | None, out_prefix: str | None) -> dict:
    summary = f"{model} 要用 `{cli}` 跑，但协作台还没有这个 CLI 的执行适配，没有执行"
    err = ""
    if out_prefix:
        err_path = Path(str(Path(out_prefix).resolve()) + ".err.txt")
        err_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.write_text(summary, encoding="utf-8")
        err = str(err_path)
    return {"ok": False, "exit": 127, "timedout": False, "seconds": 0, "codex": "",
            "workspace": str(Path(workspace or ROOT).resolve()), "last": "", "stdout": "",
            "stderr": err, "summary": summary, "model": model, "reasoning_effort": "",
            "tokens": None, "scratch_removed": 0, "agent": cli}


def run_codex(
    prompt: str,
    workspace: str | None = None,
    out_prefix: str | None = None,
    sandbox: str = "workspace-write",
    timeout: int = 1200,
    model: str = "",
    reasoning_effort: str = "",
    fallback_models: list[str] | None = None,
) -> dict:
    """跑一次 Codex，返回结果字典（不抛异常，失败用 ok=False 表达）。

    这里也是所有角色调模型的唯一入口：注册表里这个模型的 `cli` 是 claude，就转给
    `claude_bridge`；是别的、协作台还没有执行适配的 CLI，就如实失败，不拿 codex 硬跑。

    模型容量满载（`at capacity`）时按 `fallback_models` 依次降级——每个候选模型
    各自按注册表路由，所以降级链里混着别的 CLI 的型号也跑得对。
    """
    models_to_try = fallback.attempt_models(model, fallback_models)

    def attempt(try_model: str, index: int) -> dict:
        route = _route(try_model)
        if route == "claude":
            from .claude_bridge import _run_claude_once
            return _run_claude_once(
                prompt, workspace=workspace, out_prefix=out_prefix, sandbox=sandbox,
                timeout=timeout, model=try_model, reasoning_effort=reasoning_effort,
                attempt=index)
        if route == "api":
            from .api_bridge import run_api
            return run_api(
                prompt, workspace=workspace, out_prefix=out_prefix, sandbox=sandbox,
                timeout=timeout, model=try_model, reasoning_effort=reasoning_effort,
                attempt=index)
        if route != "codex":
            return _no_adapter(route, try_model, workspace, out_prefix)
        return _run_codex_once(
            prompt=prompt,
            workspace=workspace,
            out_prefix=out_prefix,
            sandbox=sandbox,
            timeout=timeout,
            model=try_model,
            reasoning_effort=reasoning_effort,
            attempt=index,
        )

    return fallback.run_with_fallback(attempt, models_to_try, label="codex")


def _run_codex_once(
    prompt: str,
    workspace: str | None,
    out_prefix: str | None,
    sandbox: str,
    timeout: int,
    model: str,
    reasoning_effort: str,
    attempt: int,
) -> dict:
    """执行一次 Codex 调用（内部函数，供 run_codex 降级逻辑使用）。"""
    ws = Path(workspace or ROOT).resolve()
    scratch = ws / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    if out_prefix:
        prefix = Path(out_prefix).resolve()
        # 多次尝试时加后缀避免覆盖
        if attempt > 0:
            prefix = Path(str(prefix) + f"-attempt{attempt}")
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
    provider_args, provider_env = provider_overrides(model)
    args += provider_args
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
    child_env.update(provider_env)

    started = time.time()
    try:
        rc, stdout, stderr, timedout = run_capped(
            args, cwd=str(ws), input_text=prompt, env=child_env, timeout=timeout)
    except OSError as exc:
        return {
            "ok": False, "exit": 127, "timedout": False, "seconds": 0,
            "codex": codex, "workspace": str(ws), "last": "", "stdout": "", "stderr": "",
            "summary": f"启动 Codex 失败：{exc}",
        }
    if timedout:
        stderr = f"{stderr}\n[bridge] 超时 {timeout}s，已终止（含子进程树）。"
    elapsed = int(time.time() - started)

    out_path.write_text(stdout or "", encoding="utf-8")
    err_path.write_text(stderr or "", encoding="utf-8")

    last = last_path.read_text(encoding="utf-8") if last_path.is_file() else ""
    if not last.strip():
        tail = [ln for ln in (stdout or "").splitlines() if ln.strip()][-60:]
        last = "\n".join(tail)
        last_path.write_text(last, encoding="utf-8")

    summary = next((ln.strip() for ln in last.splitlines() if ln.strip()), "")
    # 连不上端点这种失败，单独留一张便签：派发/编排据此立刻停手，而不是再空跑一轮。
    #
    # 只在**一个字都没产出**时才这么判。Codex 退出后还会顺带打一条
    # `error sending request for url (https://chatgpt.com/backend-api/ps/mcp)`——
    # 那是它自带的 MCP 通道，跟模型端点无关：T-261102-200022 就是这样，
    # 执行者其实已经改完文件并自验通过，却因为末尾这行噪声被判成"连不上"。
    endpoint = str(_provider_spec(model).get("base_url") or "")
    env_hint = ""
    if not last.strip():
        env_hint = classify_env_failure(stderr=stderr or "", stdout=stdout or "",
                                        summary=summary, timedout=timedout, endpoint=endpoint)
    hint_path = Path(str(prefix) + ".hint.txt")
    if env_hint:
        hint_path.write_text(env_hint + "\n", encoding="utf-8")
    # 收掉沙箱留下的空临时目录，别把工作区搞成"一堆不知名的东西"
    scratch_removed = prune_scratch(ws)
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
        "scratch_removed": scratch_removed,
        "env_error": env_hint,
        "hint": str(hint_path) if env_hint else "",
        "endpoint": endpoint,
    }


def prune_scratch(workspace: str | Path | None) -> int:
    """收掉 Codex 沙箱在工作区里留下的空临时目录，返回删掉的目录数。

    为了让沙箱里的临时文件能写（见下面 ``TEMP`` 那段），临时目录被指到
    ``<工作区>/.tmp``。Codex 每跑一条命令就在里面建一个 ``.tmpXXXXXX`` 空目录，
    跑完不回收入——一次任务能堆几十个，用户打开工作区就是"一堆不知名的东西"。

    只删**空目录**，一个文件都不碰（产物绝不会被误删）；``.tmp`` 自己也空了就一并删掉。
    没给工作区就直接返回 0——不猜、更不顺手清自己的仓库。
    """
    if not workspace:
        return 0
    try:
        scratch = Path(workspace) / ".tmp"
    except (TypeError, ValueError):
        return 0
    if not scratch.is_dir():
        return 0
    removed = 0
    try:
        children = list(scratch.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if not child.is_dir() or any(child.iterdir()):
                continue
            child.rmdir()
            removed += 1
        except OSError:      # 正被别的进程占着：留着，下一轮再收
            continue
    try:
        if not any(scratch.iterdir()):
            scratch.rmdir()
    except OSError:
        pass
    return removed


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
