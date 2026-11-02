#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""大脑 / 执行者协作协议命令行。

大脑（DeepSeek Harness 会话）写任务卡、计划、验收结论；执行者（Codex）按任务卡干活。
本脚本管协议落地：任务卡存取、把任务+计划交给 Codex、跑验收命令、出报告。

用法:
  python bin/team.py new --title "..." --goal "..." --acceptance "..." --verify "python -m unittest ..." --scope tools/ tests/
  （verify 按任务产出写：代码用 unittest/pytest，静态文件用 `python -c "..."` 直接检查产物）
  python bin/team.py dispatch -t T-260101-120000
  python bin/team.py verify   -t T-260101-120000
  python bin/team.py status
  python bin/team.py report   -t T-260101-120000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from .paths import workbench_root  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = workbench_root()
TASKS = ROOT / "tasks"

from .resources import resource  # noqa: E402
from .winproc import quiet_flags  # noqa: E402
from .codex_bridge import run_codex  # noqa: E402
from . import models  # noqa: E402
from . import verify_guard  # noqa: E402

STATUS_ORDER = {
    "planned": "已建卡",
    "executed": "已执行",
    "exec-failed": "执行失败",
    "failed": "验收失败",
    "passed": "通过",
    "stopped": "已停止",
}

# 执行者默认档位：改 config/team.json 或环境变量 EXECUTOR_MODEL 即可换模型。
CONFIG_PATH = resource("config", "team.json")
DEFAULT_EXECUTOR = {
    "model": "gpt-6.1-terra",
    "reasoning_effort": "medium",
    "timeout": 900,
    "sandbox": "workspace-write",
}

# 复核者默认档位：故意用另一个模型，换视角、也省额度。
# **sandbox 固定 read-only，改配置也降级**（见 _review_sandbox）：复核能改代码，
# 就等于"决策与执行分离"这个架构塌了——靠自觉不行，得靠技术上做不到。
DEFAULT_REVIEWER = {
    "model": "gpt-6-luna",
    "reasoning_effort": "low",
    "timeout": 600,
    "sandbox": "read-only",
}
REVIEW_SANDBOX = "read-only"


def _review_sandbox(requested: str | None, tid: str = "") -> str:
    """复核者只读是硬约束：请求可写也强制降级，并且留痕（不静默改）。

    以前这里是 `args.sandbox or cfg["sandbox"]`——默认只读，但配置或命令行一改就能写。
    那等于把安全建立在"没人会改配置"上。现在这条分支只认一个答案。
    """
    want = str(requested or "").strip()
    if not want or want == REVIEW_SANDBOX:
        return REVIEW_SANDBOX
    room_event(f"复核请求了可写沙箱（{want}），已强制降为只读", tid, "review", "bad", "reviewer")
    print(f"⚠ 复核者只能只读：请求 {want} → 强制 {REVIEW_SANDBOX}")
    return REVIEW_SANDBOX


def workspace_fingerprint(ws: Path) -> dict[str, str]:
    """工作区改动文件的指纹（路径 → 大小:mtime），用来抓"复核者动了手"。

    沙箱是技术保证，这里再加一道事后对账：即使哪天沙箱失效或配置被改，
    审完一比对就知道文件被谁动过，不至于静默通过。
    """
    prints: dict[str, str] = {}
    try:
        names = changed_files(ws)
    except (OSError, SystemExit, subprocess.SubprocessError):
        return prints
    for name in names:
        try:
            stat = (ws / name).stat()
        except OSError:
            prints[name] = "gone"
            continue
        prints[name] = f"{stat.st_size}:{stat.st_mtime_ns}"
    return prints


def _diff_fingerprint(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """两份指纹比一比：新冒出来的、被改过的、被删掉的都算「动过手」。"""
    names = set(before) | set(after)
    return sorted(name for name in names if before.get(name) != after.get(name))


def _load_role_config(role: str, defaults: dict) -> dict:
    cfg = dict(defaults)
    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        cfg.update(data.get(role) or {})
    env_model = os.environ.get(f"{role.upper()}_MODEL")
    env_effort = os.environ.get(f"{role.upper()}_EFFORT")
    if env_model:
        cfg["model"] = env_model
    if env_effort:
        cfg["reasoning_effort"] = env_effort
    return cfg


def load_executor_config() -> dict:
    return _load_role_config("executor", DEFAULT_EXECUTOR)


def load_reviewer_config() -> dict:
    return _load_role_config("reviewer", DEFAULT_REVIEWER)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def task_dir(tid: str) -> Path:
    return TASKS / tid


def new_task_id(now: datetime | None = None) -> str:
    """生成一个**不会覆盖已有卡**的任务 ID：`T-YYMMDD-HHMMSS`，同秒再加 `-2`、`-3`…

    原来只到秒，同一秒里连建两张卡会直接覆盖前一张（`mkdir(exist_ok=True)` 不拦），
    而 agent 一次对话里连着建两张卡并不罕见——静默丢卡，事后完全看不出来。
    """
    stamp = (now or datetime.now()).strftime("%y%m%d-%H%M%S")
    base = f"T-{stamp}"
    if not (TASKS / base).exists():
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if not (TASKS / candidate).exists():
            return candidate
    return f"{base}-{datetime.now().strftime('%f')}"


def load_task(tid: str) -> dict:
    path = task_dir(tid) / "task.json"
    if not path.is_file():
        raise SystemExit(f"任务不存在：{tid}（看下 python bin/team.py status）")
    return json.loads(path.read_text(encoding="utf-8"))


def save_task(task: dict) -> None:
    path = task_dir(task["id"]) / "task.json"
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_task_id(tid: str | None) -> str:
    if tid:
        return tid
    dirs = sorted((d.name for d in TASKS.iterdir() if d.is_dir()), reverse=True) if TASKS.is_dir() else []
    if not dirs:
        raise SystemExit("还没有任务卡：先用 new 创建。")
    return dirs[0]


def round_count(task: dict) -> int:
    return len(task.get("rounds") or [])


def latest_verdict(tid: str) -> str:
    files = sorted(task_dir(tid).glob("verdict-*.md"), reverse=True)
    return files[0].read_text(encoding="utf-8") if files else ""


def task_workspace(task: dict) -> Path:
    """任务卡里声明的产物工作区；没写就用工具仓库根目录。"""
    raw = (task.get("workspace") or "").strip()
    ws = Path(raw).expanduser().resolve() if raw else ROOT
    if not ws.is_dir():
        raise SystemExit(f"任务工作区不存在：{ws}（用 new --workspace 指定，或先建目录）")
    return ws


def build_prompt(task: dict, round_no: int) -> str:
    executor = resource("agents", "executor.md").read_text(encoding="utf-8")
    plan_file = task_dir(task["id"]) / "plan.md"
    plan = plan_file.read_text(encoding="utf-8") if plan_file.is_file() else "（无单独计划，按目标与验收标准自行拆步执行。）"
    scope = "、".join(task.get("scope") or []) or "（未限定，保持最小改动）"
    acceptance = "\n".join(f"- {a}" for a in task.get("acceptance") or [])
    verify = "\n".join(f"- `{v}`" for v in task.get("verify") or [])

    prompt = f"""{executor}

## 本次任务（第 {round_no} 轮，共最多 {task.get('max_rounds', 3)} 轮）
- 任务 ID：{task['id']}
- 标题：{task['title']}
- 目标：{task['goal']}
- 允许修改的路径：{scope}

## 验收标准（必须逐条满足）
{acceptance}

## 验收命令（你必须自己跑一遍，并把关键输出贴出来）
{verify}

## 大脑的计划
{plan}
"""
    verdict = latest_verdict(task["id"])
    if verdict:
        prompt += f"""
## 上一轮验收结论（必须针对它修复，不要重做无关部分）
{verdict}
"""
    prompt += f"""
## 关键约束
- 当前工作目录：{task_workspace(task)}
- 只改上面允许的路径；不改 tasks/、agents/、bin/ 里的协作系统文件。
- 不安装第三方依赖，不联网下载。
- 不确定就说明不确定，禁止编造已验证的结果。
"""
    return prompt


def room_event(text: str, tid: str, phase: str, state: str, actor: str = "system") -> None:
    """往群聊里发一条流水线事件（协作台据此显示实时状态）；失败不影响主流程。"""
    try:
        import room  # 同目录
        room.post("system", text, "event",
                  {"phase": phase, "state": state, "actor": actor, "task": tid})
    except Exception:  # noqa: BLE001
        pass


def cmd_new(args: argparse.Namespace) -> int:
    if not args.acceptance:
        raise SystemExit("new 需要至少一条 --acceptance（可验收判据）")
    if not args.verify:
        raise SystemExit("new 需要至少一条 --verify（可执行命令）")
    workspace = ROOT
    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
        if not workspace.exists():
            raise SystemExit(f"--workspace 指向的路径不存在：{workspace}（先建目录，别指望它自动创建）")
        if not workspace.is_dir():
            raise SystemExit(f"--workspace 必须是目录，现在指向的是文件：{workspace}")
    # 校验通过后才建目录：参数非法时不留空任务卡
    tid = new_task_id()
    tdir = task_dir(tid)
    tdir.mkdir(parents=True, exist_ok=True)
    task = {
        "id": tid,
        "title": args.title,
        "goal": args.goal,
        "owner": args.owner,
        "status": "planned",
        "scope": list(args.scope or []),
        "acceptance": list(args.acceptance),
        "verify": list(args.verify),
        "max_rounds": args.max_rounds,
        "note": args.note or "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "workspace": str(workspace),
        "rounds": [],
    }
    override = {k: v for k, v in (("model", args.model), ("effort", args.effort)) if v}
    if override:
        task["executor"] = override
    save_task(task)
    room_event(f"建了任务卡 {tid}（{args.title}）", tid, "plan", "ok", "deepseek")
    print(f"已创建任务卡 {tid}")
    print(f"  目录: {tdir}")
    print(f"  下一步: 写 {tdir / 'plan.md'}（大脑的计划），然后 dispatch")
    print(tid)
    return 0


def workspace_untracked(workspace: Path) -> list[str]:
    """任务开始前工作区里就存在的未跟踪文件；复核时不应算作本轮改动。"""
    try:
        proc = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                              cwd=str(workspace), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=quiet_flags())
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (proc.stdout or "").splitlines():
        if line[:2].strip() == "??":
            out.append(line[3:].strip())
    return sorted(out)


def _snapshot_workspace(task: dict, tid: str) -> None:
    """记下任务开始前工作区已有的未提交改动，复核时据此区分"谁改的"。"""
    ws = task_workspace(task)
    directory = task_dir(tid)
    try:
        diff = subprocess.run(["git", "diff"], cwd=str(ws), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", creationflags=quiet_flags())
        (directory / "baseline.diff").write_text(diff.stdout or "", encoding="utf-8")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(ws), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=quiet_flags())
        task["workspace_dirty_before"] = len([line for line in (status.stdout or "").splitlines() if line.strip()])
    except (OSError, subprocess.SubprocessError):
        pass


def cmd_dispatch(args: argparse.Namespace) -> int:
    tid = resolve_task_id(args.task)
    task = load_task(tid)
    reopened = bool(task.pop("repair_pending", False))
    if task["status"] in ("passed", "stopped") and not reopened:
        raise SystemExit(f"任务 {tid} 状态为 {task['status']}，不再派发；有后续需求就开新卡。")
    if reopened:
        # 复核没过 -> 大脑要求返工：把卡退回到可派发状态，重新计一轮
        task["status"] = "planned"
        save_task(task)

    round_no = round_count(task) + 1
    if round_no > int(task.get("max_rounds", 3)):
        task["status"] = "stopped"
        task["updated_at"] = now_iso()
        save_task(task)
        raise SystemExit(f"已达最大轮次 {task.get('max_rounds', 3)}，任务标记为 stopped；由大脑决定是否换卡。")

    task["workspace_baseline"] = workspace_untracked(task_workspace(task))
    _snapshot_workspace(task, tid)

    tdir = task_dir(tid)
    # 上一轮判过"验收脚本坏了"的结论先清掉：大脑可能已经改过命令，重派时要重新体检
    verify_guard.marker_path(tdir).unlink(missing_ok=True)
    # 派发前先给验收命令做体检：命令自己崩了（正则假设了不存在的符号、变量名写错……）
    # 就别派执行者——改代码不可能让这种命令通过，只会白烧一轮。
    broken = preflight_verify_scripts(task, tdir)
    if broken:
        note = "验收命令自身报错，已停手（未派执行者）：" + "；".join(
            f"{item['command']} → {item['reason']}" for item in broken)
        task["status"] = "failed"
        task["note"] = note
        task["updated_at"] = now_iso()
        save_task(task)
        room_event(f"验收命令自身报错，没派执行者就停下：{broken[0]['reason']}", tid, "verify", "bad", "deepseek")
        print(f"⚠ 验收命令自身报错（{broken[0]['reason']}），不派执行者；详情见 "
              f"{tdir / verify_guard.BROKEN_FILE}")
        return 3

    prompt = build_prompt(task, round_no)
    prompt_path = tdir / f"prompt-{round_no:02d}.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    # 上一轮留下的"连不上端点"便签先清掉：同一轮重派时不该拿旧便签判死
    (tdir / f"exec-{round_no:02d}.hint.txt").unlink(missing_ok=True)

    cfg = load_executor_config()
    task_exec = task.get("executor") or {}
    # 优先级：命令行 --model > 卡上指定 > 运行时切换（room/model_overrides.json）> 配置文件 > 默认
    model, model_source = models.model_for("executor", task)
    if not args.model and task_exec.get("model"):
        model_source = "卡"
    model = args.model or model or cfg["model"]
    effort = args.effort or task_exec.get("effort") or models.effort_for("executor") or cfg["reasoning_effort"]
    timeout = args.timeout or cfg["timeout"]
    sandbox = args.sandbox or cfg["sandbox"]

    baseline = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(task_workspace(task)), capture_output=True, encoding="utf-8", errors="replace", creationflags=quiet_flags()).stdout.splitlines()
    task["baseline_untracked"] = [p for p in baseline if p.strip()]
    if task_workspace(task) != ROOT:
        # 记下派发前协作仓库的状态，验收时只追究这之后新出现的串味
        task["ledger_baseline"] = ledger_snapshot()
        save_task(task)

    # 容量降级链：执行者容量满载时按顺序换的备用模型（排掉本轮实际用的主模型）
    fallback_chain = models.fallback_models("executor", exclude=model or "")

    print(f"产物工作区: {task_workspace(task)}")
    print(f"协作账本:   {ROOT}")
    room_event(f"开始第 {round_no} 轮执行 {tid}（{model or '默认模型'} / {effort or '默认档'}"
               + (f" / {model_source}指定" if model_source and model_source != "配置" else "") + "）",
               tid, "dispatch", "run", "codex")
    print(f"第 {round_no} 轮派发 → Codex（模型: {model or '默认'}（{model_source}），"
          f"推理档: {effort or '默认'}，超时 {timeout}s）")
    if fallback_chain:
        print(f"  容量降级链: {' → '.join(fallback_chain)}")
    result = run_codex(
        prompt,
        workspace=str(task_workspace(task)),
        out_prefix=str(tdir / f"exec-{round_no:02d}"),
        sandbox=sandbox,
        timeout=timeout,
        model=model or "",
        reasoning_effort=effort or "",
        fallback_models=fallback_chain,
    )

    task.setdefault("rounds", []).append({
        "n": round_no,
        "at": now_iso(),
        "ok": bool(result["ok"]),
        "exit": int(result["exit"]),
        "timedout": bool(result["timedout"]),
        "seconds": int(result["seconds"]),
        "prompt": prompt_path.name,
        "result": Path(result["last"]).name if result["last"] else "",
        "summary": result["summary"],
        "model": result.get("model", "") or model,
        "model_requested": model,
        "model_source": model_source,
        "effort": result.get("reasoning_effort", ""),
        "tokens": result.get("tokens"),
        "fallback_note": result.get("fallback_note", ""),
        "env_error": str(result.get("env_error") or ""),
        "endpoint": str(result.get("endpoint") or ""),
    })
    task["status"] = "executed" if result["ok"] else "exec-failed"
    task["updated_at"] = now_iso()
    save_task(task)

    fallback_msg = f" [{result.get('fallback_note')}]" if result.get("fallback_note") else ""
    room_event(f"执行结束 {tid} 第 {round_no} 轮：退出码 {result['exit']}，{result['seconds']}s，"
               f"{result.get('tokens') or '?'} tokens{fallback_msg}",
               tid, "dispatch", "ok" if result["ok"] else "bad", "codex")
    print(f"退出码: {result['exit']}  耗时: {result['seconds']}s  用量: {result.get('tokens') or '未取到'} tokens")
    if result.get("fallback_note"):
        print(f"⚠ {result['fallback_note']}")
    print(f"产物: {result['last'] or '（无）'}")
    if not result["ok"]:
        print(f"执行者未正常结束，看日志: {result['stderr']}")
    env_error = str(result.get("env_error") or "")
    if env_error:
        # 端点不通是环境问题：换模型/返工都救不了，立刻说清楚，别让人以为卡住了
        room_event(f"执行者连不上模型端点：{env_error}", tid, "dispatch", "bad", "codex")
        print(f"⚠ {env_error}")
        print(f"   端点: {result.get('endpoint') or '（Codex 默认）'}")
        print(f"   说明: {result.get('hint') or result.get('stderr')}")
    report_scope_now(task, round_no)          # 越界当场说，别等验收
    print(f"下一步: 大脑审阅产物后跑验收 → python bin/team.py verify -t {tid}")
    return 0 if result["ok"] else 1


def report_scope_now(task: dict, round_no: int) -> list[str]:
    """执行完**立刻**核对 scope，越界文件当场列出来（不等验收）。

    验收阶段还会再判一次 FAIL，但那是「已经烧掉一整轮之后」；这里早一步说出来，
    大脑可以当轮就修，用户也立刻知道有东西跑到声明范围外了。
    只报告不拦截：拦截由验收闸门负责，这里的作用是「快失败 + 留证据」。
    """
    tid = str(task.get("id") or "")
    try:
        violations = scope_violations(task, task_workspace(task))
    except (Exception, SystemExit):  # noqa: BLE001 - 核对失败不该让派发失败
        return []
    if violations:
        task["out_of_scope"] = violations
        task["updated_at"] = now_iso()
        save_task(task)
        room_event(f"⚠ 第 {round_no} 轮有 {len(violations)} 个越界改动："
                   + "、".join(violations[:5]) + ("…" if len(violations) > 5 else ""),
                   tid, "dispatch", "bad", "codex")
        print(f"⚠ 越界改动（不在任务卡 scope 内）：{', '.join(violations)}")
    elif task.get("out_of_scope"):
        task.pop("out_of_scope", None)        # 上一轮越界的文件被改回来了，标记跟着清掉
        save_task(task)
    return violations


LEDGER_PREFIXES = ("tasks/", "room/", "docs/", ".codex-home/", ".tmp/", "config/", "agents/", "bin/")


def ledger_snapshot() -> dict[str, str]:
    """记下协作仓库当前有哪些未提交改动（路径 → 内容指纹）。

    dispatch 前存一份，verify 时对比：只有「这之后新冒出来/被改过」的账本外文件才判串味。
    否则大脑自己正在改的东西（比如新写的测试）会被误当成执行者写错地方。
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ROOT),
            capture_output=True, encoding="utf-8", errors="replace", creationflags=quiet_flags())
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    snapshot: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"').replace("\\", "/")
        absolute = ROOT / path
        try:
            digest = hashlib.sha1(absolute.read_bytes()).hexdigest() if absolute.is_file() else "dir"
        except OSError:
            digest = "missing"
        snapshot[path] = f"{line[:2].strip()}:{digest}"
    return snapshot


def collab_stray_changes(baseline: dict[str, str] | None = None,
                         scope: list[str] | None = None) -> list[str]:
    """检查协作仓库里有没有本该落在项目工作区的代码改动。

    只把「命中任务 scope 的改动」算串味：开发者自己在协作仓库里改
    agent_team/、tests/、docs/ 是正常开发行为，不该被判成失败。
    没声明 scope 时保持旧行为（任何非账本改动都算），因为那时无法判断边界。
    """
    scope_norm = [str(item).strip().replace("\\", "/").rstrip("/")
                  for item in (scope or [])]
    scope_norm = [item for item in scope_norm if item]
    stray = []
    for path, mark in ledger_snapshot().items():
        if path.startswith(LEDGER_PREFIXES) or path.endswith(".pyc") or "__pycache__" in path:
            continue
        if baseline and baseline.get(path) == mark:
            continue  # dispatch 之前就有、且没被改过 —— 不是这次的锅
        if scope_norm and not any(path == item or path.startswith(item + "/") for item in scope_norm):
            continue  # 不在任务 scope 里：这是开发者自己的改动
        stray.append(path)
    return stray


def _unquoted_metachar(command: str) -> str:
    """引号外的第一个 shell 元字符；引号里的 `;` 是 `python -c "..."` 的参数，不算。"""
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "|&;<>$`\n":
            return ch
        i += 1
    return ""


def _split_argv(command: str) -> list[str]:
    """按引号切参数：去掉成对的引号，保留反斜杠（Windows 路径要原样传下去）。"""
    parts: list[str] = []
    cur: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(command) and command[i + 1] in '"\\':
                i += 1
                cur.append(command[i])
            elif ch == quote:
                quote = ""
            else:
                cur.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if cur:
                parts.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
        i += 1
    if quote:
        raise SystemExit("验收命令的引号没有闭合")
    if cur:
        parts.append("".join(cur))
    return parts


def parse_verify_argv(command: "str | list") -> list[str]:
    """把验收命令变成 argv 列表，绝不交给 shell。

    支持三种写法：
      - 已有列表（结构化）：直接逐项传参；
      - JSON 数组字符串：`'["python", "-m", "unittest"]'`；
      - 普通命令行：按引号切分，只拒绝**引号外**的 shell 元字符。引号里的
        `;`、`(`、`>` 都是参数本身（`python -c "import sys; ..."` 要靠它），
        不进 shell 就不会被解释，拦掉了反而让 HTML/文档类任务没法写验收命令。
    """
    if isinstance(command, (list, tuple)):
        argv = [str(part) for part in command]
        if not argv:
            raise SystemExit("验收命令为空")
        return argv
    text = str(command or "").strip()
    if not text:
        raise SystemExit("验收命令为空")
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"验收命令是 JSON 数组格式但解析失败：{exc}") from exc
        if not isinstance(data, list) or not data:
            raise SystemExit("验收命令的 JSON 必须是非空数组，例如 [\"python\",\"-m\",\"unittest\"]")
        return [str(part) for part in data]
    bad = _unquoted_metachar(text)
    if bad:
        raise SystemExit(
            "验收命令里出现了引号外的 shell 元字符 " + bad
            + "，本工具不通过 shell 执行。请改成结构化写法，例如：\n"
            '  --verify-json \'["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]\'\n'
            "或者把要传给程序的符号放进引号里（如 `python -c \"import sys; ...\"`）。")
    return _split_argv(text)


# 构建/缓存产物不算越界改动：它们不是人写的代码，也不该拖累验收
NOISE_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
              "node_modules", ".venv", "venv", ".tox", ".idea", ".vscode")
NOISE_SUFFIX = (".pyc", ".pyo", ".pyd", ".swp", ".log", ".tmp")
NOISE_FILES = (".DS_Store", "Thumbs.db", "desktop.ini")


def is_noise_path(name: str) -> bool:
    """缓存、编译产物、编辑器临时文件——不算 scope 越界。"""
    parts = name.replace("\\", "/").split("/")
    if any(part in NOISE_DIRS for part in parts[:-1]):
        return True
    if parts[-1] in NOISE_FILES or parts[-1].endswith(NOISE_SUFFIX):
        return True
    return ".egg-info/" in name or name.endswith(".egg-info")


def changed_files(ws: Path) -> list[str]:
    """工作区相对 git 根的改动文件（含未跟踪，已剔除缓存产物），用于 scope 核对。"""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=str(ws),
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=quiet_flags())
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        name = line[3:].strip().strip('"')
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        name = name.replace("\\", "/")
        if is_noise_path(name):
            continue
        out.append(name)
    return out


def workspace_products(task: dict) -> list[dict]:
    """这张卡在产物工作区里真正生成/改动的文件，按「是不是清单内」标好。

    返回值 ``[{"path": 相对工作区, "ref": "workspace:<相对路径>", "in_scope": bool}]``：

    - ``ref`` 是给网页用的引用格式（``room_web`` 认 ``workspace:`` 前缀），
      聊天里那个蓝色文件名点开就是它；
    - 账本目录（``room/`` ``tasks/`` ``.tmp/`` ``.codex-home/`` …）和缓存产物
      （``__pycache__`` / ``*.pyc`` / ``*.log``）**不算产物**，``changed_files`` 已经滤掉了；
    - 没写 scope 的卡一律算清单内（没声明就不限制，跟 ``scope_violations`` 一致）。
    """
    ws = task_workspace(task)
    scope = [str(item).strip().replace("\\", "/").rstrip("/")
             for item in (task.get("scope") or [])]
    scope = [item for item in scope if item]
    products: list[dict] = []
    for name in changed_files(ws):
        in_scope = (not scope) or any(name == item or name.startswith(item + "/") for item in scope)
        products.append({"path": name, "ref": f"workspace:{name}", "in_scope": in_scope})
    return products


def scope_violations(task: dict, ws: Path) -> list[str]:
    """用真实 diff 核对 scope：没写 scope 就不限制，写了就必须落进去。"""
    scope = [str(item).strip().replace("\\", "/").rstrip("/") for item in (task.get("scope") or [])]
    scope = [item for item in scope if item]
    if not scope:
        return []
    bad: list[str] = []
    for name in changed_files(ws):
        if any(name == item or name.startswith(item + "/") for item in scope):
            continue
        bad.append(name)
    return bad


def preflight_verify_scripts(task: dict, tdir: Path, timeout: int = 120) -> list[dict]:
    """建卡/派发前给验收命令做体检，返回"命令自己崩了"的那些。

    只抓崩溃类（SyntaxError/AttributeError/KeyError…），断言没过不算——那是
    "还没实现"，属于正常未达标；产物不存在（FileNotFoundError）也不算。
    """
    pairs = [(cmd, parse_verify_argv(cmd)) for cmd in task.get("verify") or []]
    if not pairs:
        return []
    broken = verify_guard.probe_all(pairs, task_workspace(task), timeout=timeout)
    if broken:
        verify_guard.marker_path(tdir).write_text(verify_guard.render_broken(broken),
                                                  encoding="utf-8")
    return broken


def cmd_verify(args: argparse.Namespace) -> int:
    tid = resolve_task_id(args.task)
    task = load_task(tid)
    tdir = task_dir(tid)
    round_no = max(1, round_count(task))
    lines = [f"# 验收轮次 {round_no} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    all_ok = True
    crashed: list[dict] = []

    for cmd in task.get("verify") or []:
        started = time.time()
        room_event(f"跑验收命令：{cmd}", tid, "verify", "run", "deepseek")
        print(f"运行验收命令: {cmd}")
        argv = parse_verify_argv(cmd)
        proc = subprocess.run(
            argv, shell=False, cwd=str(task_workspace(task)),
            capture_output=True, encoding="utf-8", errors="replace", creationflags=quiet_flags())
        code = proc.returncode
        elapsed = int(time.time() - started)
        all_ok = all_ok and code == 0
        output = (proc.stdout or "") + (proc.stderr or "")
        lines += [f"## `{cmd}`", f"- argv: `{argv}`", f"- 退出码: {code}  耗时: {elapsed}s", "```",
                  output, "```", ""]
        reason = "" if code == 0 else verify_guard.crash_reason(output)
        if reason:
            crashed.append({"command": cmd, "argv": argv, "code": code, "reason": reason,
                            "output": output, "seconds": elapsed})
            lines += [f"**验收命令自身报错**：{reason}（不是代码没写对）", ""]
        print(f"  退出码 {code}，耗时 {elapsed}s" + (f"，脚本崩溃：{reason}" if reason else ""))

    out_of_scope = scope_violations(task, task_workspace(task))
    if out_of_scope:
        all_ok = False
        lines += ["## 越界改动（阻塞）",
                  "以下文件的改动不在任务卡声明的 scope 内：",
                  *[f"- `{name}`" for name in out_of_scope],
                  "", "要么把这些改动移出本轮，要么先扩 scope 再重跑。", ""]
        print(f"  ⚠ {len(out_of_scope)} 个文件越界，判 FAIL")

    stray = (collab_stray_changes(task.get("ledger_baseline"), task.get("scope"))
             if task_workspace(task) != ROOT else [])
    if stray:
        all_ok = False
        lines += ["## 工作区串味（阻塞）",
                  f"产物工作区是 `{task_workspace(task)}`，但协作仓库里也出现了改动：",
                  *[f"- `{p}`" for p in stray],
                  "这些改动不该出现在协作仓库，必须挪回项目工作区或撤销。", ""]
        print(f"  ⚠ 协作仓库出现 {len(stray)} 处不该有的改动，判 FAIL")

    lines += ["## 结论", "全部验收命令通过。" if all_ok else "有验收命令失败，需要修复轮次。"]
    verify_path = tdir / f"verify-{round_no:02d}.txt"
    verify_path.write_text("\n".join(lines), encoding="utf-8")

    task["status"] = "passed" if all_ok else "failed"
    task["updated_at"] = now_iso()
    if crashed and not all_ok:
        # 崩溃的是验收命令自己：让大脑改命令，别给执行者派返工
        verify_guard.marker_path(tdir).write_text(verify_guard.render_broken(crashed, round_no),
                                                 encoding="utf-8")
        task["note"] = "验收命令自身报错（" + "；".join(
            f"{item['command']} → {item['reason']}" for item in crashed) + "）"
        save_task(task)
        room_event(f"验收命令自身报错，不派返工：{crashed[0]['reason']}", tid, "verify", "bad", "deepseek")
        print(f"⚠ 验收命令自身报错（{crashed[0]['reason']}），判为脚本问题，不派返工")
        print(f"证据: {verify_guard.marker_path(tdir)}")
        return 2
    save_task(task)

    room_event(f"验收 {'通过' if all_ok else '未通过'}（{tid} 第 {round_no} 轮）",
               tid, "verify", "ok" if all_ok else "bad", "deepseek")
    print(("验收结果: PASS" if all_ok else "验收结果: FAIL"))
    print(f"证据: {verify_path}")
    if not all_ok:
        print(f"下一步: 大脑写 {tdir / f'verdict-{round_no:02d}.md'}（repair），再 dispatch 下一轮")
    return 0 if all_ok else 1


def _git_diff(limit: int = 60000, cwd: Path | None = None, skip_untracked: set[str] | None = None) -> str:
    """取相对 HEAD 的完整 diff，包含未跟踪的新文件，过长则截断。"""
    where = str(cwd or ROOT)

    def git(*args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=where,
                capture_output=True, encoding="utf-8", errors="replace", creationflags=quiet_flags())
            return proc.stdout if proc.returncode == 0 else ""
        except OSError:
            return ""

    # 账本目录不属于任何任务的产物：它们每跑一步都会变，不该算「越界改动」
    ledger = (":(exclude)room", ":(exclude)tasks", ":(exclude).codex-home", ":(exclude).tmp")
    diff = git("diff", "HEAD", "--", ".", *ledger)
    skip = {n.replace("\\", "/") for n in (skip_untracked or set())}
    untracked = [p for p in git("ls-files", "--others", "--exclude-standard").splitlines()
                 if p.strip() and p.replace("\\", "/") not in skip
                 and not p.replace("\\", "/").startswith(("room/", "tasks/", ".codex-home/", ".tmp/"))
                 # 缓存/编译产物不进复核材料，否则复核者会为一堆 .pyc 要求返工
                 and not is_noise_path(p)]
    new_file_diffs = []
    for name in untracked[:40]:
        # 未跟踪文件不在 git diff 里，单独生成一份，否则复核者看不到新增文件
        piece = git("diff", "--no-index", "--", "NUL", name)
        if piece.strip():
            new_file_diffs.append(piece)
    if new_file_diffs:
        diff = (diff + "\n" + "\n".join(new_file_diffs)).strip()
    if not diff.strip():
        diff = git("show", "--format=", "HEAD")
        if untracked:
            names = "\n".join(f"- {n}" for n in untracked[:40])
            diff = f"{diff}\n\n（以下新文件无内容差异可展示，请直接打开查看）\n{names}"
    if len(diff) > limit:
        diff = diff[:limit] + "\n\n…（diff 过长，已截断，完整内容请自己用 git diff 查看）"
    return diff.strip() or "（工作区没有未提交改动）"


def build_review_prompt(task: dict, round_no: int, diff: str) -> str:
    reviewer = resource("agents", "reviewer.md").read_text(encoding="utf-8")
    acceptance = "\n".join(f"- {a}" for a in task.get("acceptance") or [])
    verify = "\n".join(f"- `{v}`" for v in task.get("verify") or [])
    scope = "、".join(task.get("scope") or []) or "（未限定）"
    ws = task_workspace(task)
    baseline = "、".join(task.get("workspace_baseline") or []) or "（无）"
    dirty = task.get("workspace_dirty_before") or 0
    return f"""{reviewer}

## 被复核的任务
- 任务 ID：{task['id']}
- 标题：{task['title']}
- 目标：{task['goal']}
- 声明的改动范围：{scope}
- 第 {round_no} 轮执行

## 验收标准（逐条核对实际代码是否满足）
{acceptance}

## 验收命令（已由大脑跑过，你可以重跑只读部分）
{verify}

- 任务开始前就存在、并非本轮引入的未跟踪文件：{baseline}
- 任务开始前工作区已有 {dirty} 处未提交改动（原文见 `tasks/<任务ID>/baseline.diff`）：
  这些是上一阶段留下的，**不是本轮引入的**，判断"越界改动"时请先对照它。

## 执行者本轮的改动（git diff）
```diff
{diff}
```

## 工作要求
- 工作目录：{ws}
- 账本目录 `room/`、`tasks/`、`.codex-home/`、`.tmp/` 是协作流水账，已从上面的 diff 里排除；
  它们变来变去是正常的，**不要**把它们当成越界改动来报。
- 构建产物（`__pycache__/`、`*.pyc`、`.pytest_cache/`、`node_modules/` 等）不是人写的代码，
  已从 diff 里排除；即便在 `git status` 里看到，也**不要**据此要求返工。
- 你只能读，不能改任何文件。
- 需要更多上下文就自己打开相关文件看，别猜。
- 严格按 reviewer 契约的格式输出结论（PASS 或 NEEDS_FIX）。"""


def cmd_review(args: argparse.Namespace) -> int:
    tid = resolve_task_id(args.task)
    task = load_task(tid)
    tdir = task_dir(tid)
    round_no = max(1, round_count(task))

    ws = task_workspace(task)
    diff = _git_diff(cwd=ws, skip_untracked=set(task.get("baseline_untracked") or []))
    prompt = build_review_prompt(task, round_no, diff)
    prompt_path = tdir / f"review-prompt-{round_no:02d}.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    cfg = load_reviewer_config()
    model, model_source = models.model_for("reviewer", task)
    model = args.model or model or cfg["model"]
    effort = args.effort or models.effort_for("reviewer") or cfg["reasoning_effort"]
    timeout = args.timeout or cfg["timeout"]
    sandbox = _review_sandbox(args.sandbox or cfg["sandbox"], tid)
    before = workspace_fingerprint(ws)         # 审之前拍张照，审完对一次

    room_event(f"开始复核 {tid}（{model} / 只读）", tid, "review", "run", "reviewer")
    attempts = [model] + [m for m in (cfg.get("fallback_models") or []) if m and m != model]
    result = None
    text = ""
    used_model = model
    for index, candidate in enumerate(attempts):
        print(f"复核第 {round_no} 轮 → Codex（模型: {candidate}，推理档: {effort}，只读）")
        result = run_codex(
            prompt,
            workspace=str(ws),
            out_prefix=str(tdir / f"review-{round_no:02d}-{index}"),
            sandbox=sandbox,
            timeout=timeout,
            model=candidate,
            reasoning_effort=effort,
        )
        used_model = candidate
        text = ""
        if result.get("last"):
            text = Path(result["last"]).read_text(encoding="utf-8", errors="replace")
        upper = text.upper()
        if "PASS" in upper or "NEEDS_FIX" in upper:
            break
        if result.get("env_error"):
            # 复核模型连不上端点：换下一个型号还是同一个网络，别一个个空烧三分钟
            room_event(f"复核模型连不上端点：{result['env_error']}", tid, "review", "bad", "reviewer")
            print(f"⚠ {result['env_error']}")
            break
        if index + 1 < len(attempts):
            print(f"  {candidate} 没给出结论（可能模型排队/限流），换 {attempts[index + 1]} 重试一次")
    needs_fix = "NEEDS_FIX" in text.upper()
    passed = "PASS" in text.upper() and not needs_fix
    model = used_model
    # 复核没通过时，任务不能停在"通过"状态
    if not passed and task.get("status") in ("passed", "executed"):
        task["status"] = "failed"

    touched = _diff_fingerprint(before, workspace_fingerprint(ws))
    if touched:
        task["review_wrote"] = touched
        room_event("⚠ 复核者改动了工作区（只读沙箱本不该允许）："
                   + "、".join(touched[:5]), tid, "review", "bad", "reviewer")
        print(f"⚠ 复核者动了工作区：{', '.join(touched)}")

    task.setdefault("reviews", []).append({
        "n": round_no,
        "at": now_iso(),
        "ok": bool(result["ok"]),
        "verdict": "NEEDS_FIX" if needs_fix else ("PASS" if passed else "UNKNOWN"),
        "seconds": int(result["seconds"]),
        "model": result.get("model", ""),
        "tokens": result.get("tokens"),
        "file": Path(result["last"]).name if result["last"] else "",
        "sandbox": sandbox,
        "wrote": touched,
    })
    task["updated_at"] = now_iso()
    save_task(task)

    room_event(f"复核完成 {tid}：{'PASS' if passed else ('NEEDS_FIX' if needs_fix else '未判定')}"
               f"，{result['seconds']}s", tid, "review", "ok" if passed else "bad", "reviewer")
    print(f"耗时 {result['seconds']}s  用量: {result.get('tokens') or '未取到'} tokens")
    print(f"复核结论: {'NEEDS_FIX' if needs_fix else ('PASS' if passed else '未判定')}")
    print(f"全文: {result['last'] or '（无）'}")
    return 0 if passed else 1


def cmd_status(args: argparse.Namespace) -> int:
    if not TASKS.is_dir():
        print("还没有任务卡。用 new 创建。")
        return 0
    rows = [load_task(d.name) for d in sorted(TASKS.iterdir()) if d.is_dir() and (d / "task.json").is_file()]
    if not rows:
        print("还没有任务卡。用 new 创建。")
        return 0
    print(f"{'TASK':<18} {'状态':<8} {'轮次':<4} 标题")
    print("-" * 78)
    for t in rows:
        print(f"{t['id']:<18} {STATUS_ORDER.get(t['status'], t['status']):<8} {round_count(t):<4} {t['title']}")
    return 0


def _git_diff_stat(cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat", "HEAD"], cwd=str(cwd or ROOT),
            capture_output=True, encoding="utf-8", errors="replace", creationflags=quiet_flags())
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        proc = subprocess.run(
            ["git", "status", "--short"], cwd=str(cwd or ROOT),
            capture_output=True, encoding="utf-8", errors="replace", creationflags=quiet_flags())
        return proc.stdout.strip()
    except OSError:
        return "（git 不可用）"


def cmd_report(args: argparse.Namespace) -> int:
    tid = resolve_task_id(args.task)
    task = load_task(tid)
    tdir = task_dir(tid)
    out = [
        f"# 协作报告：{task['title']}", "",
        f"- 任务 ID：{task['id']}",
        f"- 目标：{task['goal']}",
        f"- 状态：**{STATUS_ORDER.get(task['status'], task['status'])}**",
        f"- 轮次：{round_count(task)} / {task.get('max_rounds', 3)}",
        f"- 执行者：{task.get('owner', 'codex')}", "",
        "## 验收标准",
    ]
    out += [f"- {a}" for a in task.get("acceptance") or []]
    out += ["", "## 轮次记录"]
    rounds = task.get("rounds") or []
    out += [f"- 第 {r['n']} 轮：ok={r['ok']} exit={r['exit']} {r['seconds']}s tokens={r.get('tokens') or '?'} — {r['summary']}" for r in rounds] or ["（尚未派发）"]
    out += ["", "## 验收证据"]
    vfiles = sorted(tdir.glob("verify-*.txt"), reverse=True)
    if vfiles:
        tail = vfiles[0].read_text(encoding="utf-8").splitlines()[-25:]
        out += [f"见 `{vfiles[0].name}`：", "", "```"] + tail + ["```"]
    else:
        out += ["（尚未验收）"]
    out += ["", "## 工作区改动", "", "```", _git_diff_stat(task_workspace(task)), "```"]

    report_path = tdir / "report.md"
    report_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"已生成 {report_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="大脑/执行者协作协议")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="创建任务卡")
    p.add_argument("--title", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--acceptance", action="append", default=[])
    p.add_argument("--verify", action="append", default=[])
    p.add_argument("--scope", action="append", default=[])
    p.add_argument("--owner", default="codex")
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--note", default="")
    p.add_argument("--model", default=None, help="这张卡指定执行者模型（覆盖 config/team.json）")
    p.add_argument("--effort", default=None, help="这张卡指定推理档 low/medium/high/xhigh/max")
    p.add_argument("--workspace", default=None,
                   help="产物工作区（默认本仓库根目录）；代码改动、git diff、CODEX_HOME 都落在这里")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("dispatch", help="把任务卡交给执行者")
    p.add_argument("-t", "--task", default=None)
    p.add_argument("--model", default=None, help="覆盖执行者模型（默认按 卡 > 运行时切换 > config/team.json 解析）")
    p.add_argument("--effort", default=None, help="推理档 low/medium/high/xhigh/max")
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--sandbox", default=None,
                   choices=["read-only", "workspace-write", "danger-full-access"])
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("verify", help="跑验收命令并记录证据")
    p.add_argument("-t", "--task", default=None)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("review", help="让复核者只读审查本轮改动")
    p.add_argument("-t", "--task", default=None)
    p.add_argument("--model", default=None, help="覆盖复核者模型（默认 gpt-6-luna）")
    p.add_argument("--effort", default=None)
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--sandbox", default=None, choices=["read-only", "workspace-write"],
                   help="复核**固定只读**：写别的会被强制降级并记一条告警（留着是为了审计谁想开）")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("status", help="任务总览")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="生成 Markdown 报告")
    p.add_argument("-t", "--task", default=None)
    p.set_defaults(func=cmd_report)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
