#!/usr/bin/env python3
"""常驻工作流引擎：3 秒轮询群聊，发现新任务就自动跑完整流水线。

流水线：规划（Codex 当参谋出任务卡）→ 派发（Codex 执行）→ 验收（本地命令）
        → 复核（Codex 只读审查）→ 报告 → 回帖

  python bin/worker.py [--interval 3] [--max-rounds 2] [--once]

设计约束：
  - 只处理 from=user 的 task/msg，认领即 ack，避免和大脑心跳重复执行；
  - 验收命令走白名单，防止规划器给出破坏性命令；
  - 单实例锁（room/worker.lock），崩溃 90 秒后自动接管；
  - 全程写事件进群聊，协作台能看到实时进度。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
ROOM = ROOT / "room"
LOCK = ROOM / "worker.lock"
NUDGE = ROOM / "nudge"            # 有新消息时被点一下，引擎立刻处理，不等轮询
TYPING = ROOM / "typing.json"     # 谁正在思考，页面据此显示"正在输入…"
LOG = ROOM / "worker.log"

from . import agents  # noqa: E402
from . import control  # noqa: E402
from . import room  # noqa: E402
from .winproc import quiet_flags  # noqa: E402
from .codex_bridge import run_codex  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SAFE_VERIFY_PREFIXES = (
    "python -m unittest",
    "python -m pytest",
    "python -m py_compile",
    "python tools/",
    "node --test",
    "npm test",
    "npx tsc",
    "npm run typecheck",
    "npm run build",
)
DEFAULT_VERIFY = "python -m unittest discover -s tests -t . -v"

PLANNER_PROMPT = """你是一个任务规划助手。根据用户这条需求，产出一个严格 JSON 任务卡（不要输出任何解释、不要 Markdown 代码块）。

用户需求：
{request}

工作区根目录：{workspace}

JSON 结构（所有字段必填）：
{
  "title": "不超过 30 字的标题",
  "goal": "目标，2-3 句话，说清要改什么、达到什么效果",
  "acceptance": ["可判真假的验收标准1", "验收标准2"],
  "verify": ["可执行的验收命令"],
  "scope": ["允许修改的相对路径或目录，如 tools/"]
}

规则：
- acceptance 每条都必须能用命令或代码检查判断真假，不要写"代码优雅"这种话。
- verify 必须能在工作区根目录直接执行，优先用 `python -m unittest discover -s tests -t . -v`；
  如果需求是纯文档改动且没有测试，用 `python -m py_compile <文件>` 之类的只读检查。
- scope 只写相对路径，禁止绝对路径，禁止写 tasks/。
- 只输出 JSON。"""

CONTINUE_PROMPT = """你是项目的阶段规划助手。用户说"{request}"，意思是接着上一阶段往下做。
你要根据下面这份"上一阶段交接材料"，规划出**下一阶段**的任务卡（严格 JSON，不要解释、不要 Markdown 代码块）。

## 上一阶段交接材料
{context}

## 规划要求
- 只规划**一轮能做扎实**的量（1-3 个文件、能跑通测试的粒度），别想一口气做完整个项目。
- 优先处理：验收没通过的用例、复核指出的问题、报告里写明"没做/有风险"的部分。
- 然后才是新功能；新功能一次只加一小块。
- 验收标准必须能用命令判真假；验收命令必须在工作区根目录能直接执行。
- 如果材料显示项目已经没有未完成项、也没有失败用例，就返回：{"done": true, "reason": "具体理由"}。

JSON 结构（正常情况，所有字段必填）：
{
  "title": "不超过 30 字，体现这一阶段做什么",
  "goal": "2-3 句话：这阶段补什么、为什么现在做它",
  "acceptance": ["可判真假的验收标准"],
  "verify": ["可执行的验收命令"],
  "scope": ["允许修改的相对路径"],
  "unfinished_after": ["这一阶段做完后预计还剩什么（没有就写 []）"]
}

只输出 JSON。"""

BANNED_VERIFY = ("rm ", "del ", "rd ", "format ", "shutdown", "reg delete", ">", "&&", "||")


def log(text: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}"
    print(line, flush=True)
    try:
        ROOM.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def event(text: str, phase: str, state: str, actor: str, tid: str = "") -> None:
    try:
        room.post("system", text, "event",
                  {"phase": phase, "state": state, "actor": actor, "task": tid})
    except Exception:  # noqa: BLE001
        pass


def say(text: str, task_id: str | None = None, status: str = "complete",
        artifacts: list[str] | None = None) -> None:
    """协调者的发言：默认 complete，失败时传 status="failed" 让界面标红。"""
    try:
        room.post("coordinator", text, "result",
                  {"task": task_id, "status": status, "artifacts": list(artifacts or [])})
    except TypeError:            # 过渡期兼容：老版 room.post 只认 (who, text, kind)
        room.post("deepseek", text, "result")
    except Exception:  # noqa: BLE001
        pass


# ---------------- 单实例锁 ----------------
def typing(on: bool, who: str = "coordinator") -> None:
    try:
        if on:
            TYPING.write_text(json.dumps({"who": who, "at": time.time()}, ensure_ascii=False),
                              encoding="utf-8")
        else:
            TYPING.unlink(missing_ok=True)
    except OSError:
        pass


def take_lock() -> bool:
    ROOM.mkdir(parents=True, exist_ok=True)
    if LOCK.is_file():
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8"))
            pid = int(info.get("pid") or 0)
            beat = float(info.get("beat") or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            pid, beat = 0, 0
        alive = False
        if pid and (time.time() - beat) < 90:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            return False
        log(f"接管过期锁（pid={pid}）")
    LOCK.write_text(json.dumps({"pid": os.getpid(), "beat": time.time(), "root": str(ROOT)}),
                    encoding="utf-8")
    return True


def beat() -> None:
    try:
        LOCK.write_text(json.dumps({"pid": os.getpid(), "beat": time.time(), "root": str(ROOT)}),
                        encoding="utf-8")
    except OSError:
        pass


# ---------------- 规划 ----------------
LEDGER_HINTS = ("tools/", "tests/", "bin/", "room/", "tasks/", "协作台", "worker.py", "team.py",
                "room_web.py", "wordstat")


def _ledger_hint_hit(text: str) -> bool:
    """需求是不是在说协作仓库自己的活。仓库名也算线索，换机器换名字自动适配。"""
    if any(hint in text for hint in LEDGER_HINTS):
        return True
    return bool(ROOT.name) and ROOT.name in text


def nearest_repo_root(path: Path) -> Path:
    """往上找最近的 git 仓库根。绑子目录会让 git diff / 验收命令全错位。"""
    current = path
    for _ in range(6):
        try:
            if (current / ".git").exists():
                return current
        except OSError:
            break
        if current.parent == current:
            break
        current = current.parent
    return path


def detect_workspace(text: str) -> tuple[Path | None, str]:
    """从需求里认出工作区。认不准就返回 (None, 原因)，绝不再默默绑到协作仓库。"""
    raw = re.findall(r"[A-Za-z]:[\\/][^\s，。；、：:;）)\]}]+", text)
    if os.name != "nt":
        # macOS / Linux：绝对路径以 / 开头，别只认盘符
        raw += re.findall(r"(?<![\w./])/(?:[^\s，。；、：:;）)\]}]+)", text)
    found: list[Path] = []
    missing: list[str] = []
    for item in raw:
        cleaned = item.strip().strip("\"'").rstrip("\\/").rstrip(".,，。；;：:）)]}")
        if len(cleaned) <= 3:
            continue
        candidate = Path(cleaned).expanduser()
        try:
            if candidate.is_dir():
                target = nearest_repo_root(candidate.resolve())
            elif candidate.is_file():
                # 点名一个文件 → 用所属仓库根当工作区（不是文件所在那层）
                target = nearest_repo_root(candidate.parent.resolve())
            else:
                missing.append(cleaned)
                continue
        except OSError:
            missing.append(cleaned)
            continue
        if target not in found:
            found.append(target)

    if len(found) == 1:
        return found[0], ""
    if len(found) > 1:
        listing = "、".join(str(d) for d in found)
        return None, f"这条提到了多个目录（{listing}），我不确定该改哪个"
    if missing:
        return None, f"这条里的路径不存在：{'、'.join(missing[:3])}"
    if _ledger_hint_hit(text):
        return ROOT, ""
    return None, "没有指定要改哪个项目（给我一个仓库目录的绝对路径，例如 " + str(ROOT.parent / "my-project") + "）"


def sanitize_plan(plan: dict) -> dict:
    verify = [v for v in (plan.get("verify") or []) if isinstance(v, str) and v.strip()]
    safe = []
    for cmd in verify:
        lowered = cmd.lower()
        if any(bad in lowered for bad in BANNED_VERIFY):
            continue
        if lowered.startswith(SAFE_VERIFY_PREFIXES):
            safe.append(cmd.strip())
    if not safe:
        safe = [DEFAULT_VERIFY]
    acceptance = [a.strip() for a in (plan.get("acceptance") or []) if str(a).strip()][:6]
    if not acceptance:
        acceptance = ["验收命令全部通过"]
    scope = []
    for item in (plan.get("scope") or []):
        item = str(item).strip().replace("\\", "/").strip("/")
        if item and not item.startswith("tasks") and ":" not in item:
            scope.append(item)
    if not scope:
        scope = ["tools/", "tests/"]
    return {
        "title": str(plan.get("title") or "").strip()[:60] or "自动任务",
        "goal": str(plan.get("goal") or "").strip() or "（规划器未给出目标）",
        "acceptance": acceptance,
        "verify": safe,
        "scope": scope,
    }


def plan_with_codex(request: str, workspace: Path) -> dict | None:
    prompt = PLANNER_PROMPT.replace("{request}", request).replace("{workspace}", str(workspace))
    out = ROOM / "planner"
    out.mkdir(parents=True, exist_ok=True)
    prefix = out / f"plan-{int(time.time())}"
    result = run_codex(
        prompt, workspace=str(workspace), out_prefix=str(prefix),
        sandbox="read-only", timeout=300, model="gpt-6.1-terra", reasoning_effort="low",
    )
    text = ""
    if result.get("last"):
        text = Path(result["last"]).read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        log(f"规划失败：没有 JSON。原始输出：{text[:300]}")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        log(f"规划 JSON 解析失败：{exc}")
        return None


# ---------------- 流水线 ----------------
def run_step(args: list[str], timeout: int, label: str) -> tuple[int, str]:
    log(f"$ {' '.join(args)}")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "team.py"), *args],
        cwd=str(ROOT), capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=quiet_flags())
    output = (proc.stdout or "") + (proc.stderr or "")
    log(f"{label} 退出码 {proc.returncode}")
    return proc.returncode, output


def control_note(kind: str, tid: str, stage: str = "") -> None:
    """把控制状态发成一条消息，用户知道引擎在等什么。"""
    text = {
        "paused": "已暂停。点「继续」我接着干，点「停止」就收工。",
        "resumed": "继续干活。",
        "waiting_approval": "计划已经出来了，你批准之后我才动手。",
        "approved": "已批准，开工。",
        "stopped": "按你的要求停下了。",
        "rerun": "按你的要求重跑这一步。",
    }.get(kind, kind)
    say(text, task_id=tid)


def step(tid: str, argv: list[str], timeout: int, label: str, stage: str) -> tuple[int, str]:
    """跑一步，跑之前先过闸门：暂停就等，停止就抛，重跑就重做这一步。"""
    while True:
        try:
            action = control.gate(
                tid, stage,
                on_event=lambda ev, st: control_note(ev, tid, st),
                heartbeat=beat)
        except control.TaskStopped:
            raise
        if action != "rerun":
            return run_step(argv, timeout, label)
        control_note("rerun", tid, stage)
        event(f"按你的要求重跑：{label}", stage, "run", "coordinator", tid)

VAGUE = {"继续", "好的", "好", "嗯", "可以", "ok", "继续吧", "下一步", "继续干"}


def do_report(tid: str) -> int:
    """就地生成报告：子进程偶发 0xC0000142（DLL 初始化失败），这里不再依赖它。"""
    try:
        from argparse import Namespace

        from . import team as team_mod
        team_mod.cmd_report(Namespace(task=tid))
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code == 0:
            return 0
    except Exception:  # noqa: BLE001
        pass
    code, _ = run_step(["report", "-t", tid], 120, "报告")
    return code


def mark_finished(tid: str) -> None:
    """复核和报告都写完才算真收工，给"等结果"的一方一个可靠信号。

    写 finished_at 的同时落一个 outcome：验收没过 / 复核要求修改的，
    终态必须是 failure，不能因为流程走完就显示成功。
    """
    card = TASKS / tid / "task.json"
    try:
        task = json.loads(card.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    reviews = task.get("reviews") or []
    verdict = (reviews[-1].get("verdict") if reviews else None) or ""
    status = task.get("status")
    # 成功终态必须有复核 PASS 作证：没复核过、复核要求修改、验收没过的，都算失败
    ok = status == "passed" and verdict == "PASS"
    if not ok and status == "passed":
        task["status"] = "failed"
    task["outcome"] = "success" if ok else "failure"
    task["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        card.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


CONTINUE_WORDS = {"继续", "继续吧", "继续干", "继续做", "接着做", "往下做", "下一步",
                  "下一阶段", "继续下一阶段", "continue", "go on", "next"}
PROJECTS = ROOM / "projects"


def is_continue_intent(text: str) -> bool:
    """只有"真的让我接着干"才算继续；带问号/冒号/句子的当聊天，别擅自开工。"""
    t = text.strip().lower().rstrip("。.!！~ ")
    if t in CONTINUE_WORDS:
        return True
    # 聊天信号：问句、解释、逗号分句，一律不当命令
    if any(mark in t for mark in ("?", "？", "：", ":", "，", ",", "、", "\n")):
        return False
    if re.search(r"[A-Za-z]:[\\/]", t) or "/" in t:
        return False
    if any(word in t for word in ("阶段", "继续做", "接下来的")):
        return True
    return t.startswith(("继续", "接着", "下一步", "下一阶段", "往下")) and len(t) <= 10


def all_cards() -> list[dict]:
    cards: list[dict] = []
    if not TASKS.is_dir():
        return cards
    for directory in TASKS.iterdir():
        card = directory / "task.json"
        if not card.is_file():
            continue
        try:
            cards.append(json.loads(card.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return cards


def project_key(workspace: Path) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "_", workspace.name) or "project"


def last_project_card() -> dict | None:
    """最近一个"业务仓库"的任务卡；协作仓库自己的活不算项目阶段。"""
    best: tuple[str, dict] | None = None
    for card in all_cards():
        ws = card.get("workspace")
        if not ws:
            continue
        try:
            if Path(ws).resolve() == ROOT.resolve():
                continue
        except OSError:
            continue
        key = card.get("updated_at") or card.get("created_at") or ""
        if best is None or key > best[0]:
            best = (key, card)
    return best[1] if best else None


def stage_number(workspace: Path) -> int:
    stages = []
    for card in all_cards():
        ws = card.get("workspace")
        if not ws:
            continue
        try:
            if Path(ws).resolve() != workspace.resolve():
                continue
        except OSError:
            continue
        stages.append(int(card.get("stage") or 1))
    return (max(stages) + 1) if stages else 1


def _read(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[:limit]
    except OSError:
        return ""


def _git(workspace: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(workspace), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=30, creationflags=quiet_flags())
        return (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def stage_progress_file(workspace: Path) -> Path:
    return PROJECTS / project_key(workspace) / "PROGRESS.md"


def build_continue_context(card: dict) -> str:
    tid = card.get("id")
    directory = TASKS / tid
    workspace = Path(card.get("workspace"))
    parts = [f"### 上一阶段任务 {tid}：{card.get('title')}",
             f"- 目标：{card.get('goal')}",
             f"- 结果：{card.get('status')}"]

    report = _read(directory / "report.md")
    if report:
        parts += ["", "### 阶段报告", report]

    verify_files = sorted(directory.glob("verify-*.txt"))
    if verify_files:
        tail = _read(verify_files[-1], 1500)
        parts += ["", f"### 最后一次验收输出（{verify_files[-1].name} 末段）", tail]

    reviews = sorted(directory.glob("review-*.last.md"))
    if reviews:
        parts += ["", "### 复核意见", _read(reviews[-1], 2000)]

    status = _git(workspace, ["status", "--short"])
    parts += ["", "### 工作区当前状态", f"```\n{status or '(干净)'}\n```"]
    log_tail = _git(workspace, ["log", "--oneline", "-5"])
    if log_tail:
        parts += ["", "### 最近提交", f"```\n{log_tail}\n```"]

    progress = _read(stage_progress_file(workspace), 4000)
    if progress:
        parts += ["", "### 历史阶段进度", progress]
    return "\n".join(parts)


def plan_continue_with_codex(request: str, context: str, workspace: Path) -> dict | None:
    prompt = (CONTINUE_PROMPT.replace("{request}", request).replace("{context}", context))
    out = ROOM / "planner"
    out.mkdir(parents=True, exist_ok=True)
    prefix = out / f"plan-continue-{int(time.time())}"
    result = run_codex(prompt, workspace=str(workspace), out_prefix=str(prefix),
                       sandbox="read-only", timeout=300, model="gpt-6.1-terra",
                       reasoning_effort="low")
    text = ""
    if result.get("last"):
        text = Path(result["last"]).read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        log(f"继续规划失败：没有 JSON。原始输出：{text[:300]}")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        log(f"继续规划 JSON 解析失败：{exc}")
        return None


def append_progress(card_id: str) -> None:
    """每阶段结束写一条交接记录，供"继续"和人工阅读。"""
    card_path = TASKS / card_id / "task.json"
    try:
        card = json.loads(card_path.read_text(encoding="utf-8"))
        workspace = Path(card.get("workspace") or ROOT)
    except (OSError, json.JSONDecodeError):
        return
    if workspace.resolve() == ROOT.resolve():
        return
    directory = TASKS / card_id
    reviews = sorted(directory.glob("review-*.last.md"))
    verdict = "未复核"
    if reviews:
        text = _read(reviews[-1], 4000).upper()
        verdict = "NEEDS_FIX" if "NEEDS_FIX" in text else ("PASS" if "PASS" in text else "未判定")
    block = [
        f"## 第 {card.get('stage') or 1} 阶段 · {card.get('title')}（{card_id}）",
        f"- 时间：{card.get('finished_at') or card.get('updated_at') or ''}",
        f"- 状态：{card.get('status')}　复核：{verdict}",
        f"- 目标：{card.get('goal')}",
    ]
    report = _read(directory / "report.md", 2000)
    if report:
        block += ["", report]
    target = stage_progress_file(workspace)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(block) + "\n\n")
    except OSError:
        pass


def latest_review_text(tid: str) -> str:
    files = [p for p in (TASKS / tid).glob("review-*.last.md") if p.is_file() and p.stat().st_size > 0]
    if not files:
        return ""
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest.read_text(encoding="utf-8", errors="replace").strip()


def write_repair_verdict(tid: str, round_no: int, findings: str) -> Path:
    body = findings or "（复核没留下正文，按测试与验收结果自查）"
    path = TASKS / tid / f"verdict-{round_no:02d}.md"
    path.write_text(
        f"# 复核未通过，返工第 {round_no} 轮\n\n## 结论\nrepair\n\n"
        f"## 必须修（来自复核者）\n{body}\n\n"
        f"## 边界\n只能改声明的范围内文件；不许改验收标准、不许删测试、不许把失败用例改成跳过。\n",
        encoding="utf-8",
    )
    return path


def handle_continue(request: str) -> None:
    """用户说"继续"：接着最近一个项目往下做，把上一阶段的证据全带上。"""
    prev = last_project_card()
    if prev is None:
        event("没找到可继续的项目", "plan", "bad", "deepseek")
        say("我这儿还没有可继续的项目。先说一句第一阶段要做什么（点名仓库目录，比如 E:\\my-project），"
            "我做完一阶段后你说「继续」就能接着往下走。")
        return

    workspace = Path(prev["workspace"]).resolve()
    stage = stage_number(workspace)
    context = build_continue_context(prev)
    event(f"接着 {workspace.name} 做第 {stage} 阶段（已读上一阶段报告、验收失败项、复核意见）",
          "plan", "run", "deepseek")

    plan = plan_continue_with_codex(request, context, workspace)
    if plan is None:
        event("继续规划失败", "plan", "bad", "deepseek")
        say(f"「继续」我接上了 {prev['id']}，但下一阶段没能规划出合法任务卡。"
            f"你可以直接说这一阶段要做什么，我按新任务走。")
        return
    if plan.get("done"):
        event("按材料判断项目已完成", "plan", "ok", "deepseek", prev["id"])
        say(f"按 {prev['id']} 的报告和验收结果，{workspace.name} 已经没有未完成项了：{plan.get('reason')}。"
            f"要做新东西就直接说需求。")
        return

    card = sanitize_plan(plan)
    card["title"] = f"[第{stage}阶段] {card['title']}"[:40]
    log(f"继续阶段 {stage}：{card['title']} | 验收 {len(card['acceptance'])} 条")

    args = ["new", "--title", card["title"], "--goal", card["goal"]]
    for item in card["acceptance"]:
        args += ["--acceptance", item]
    for cmd in card["verify"]:
        args += ["--verify", cmd]
    for item in card["scope"]:
        args += ["--scope", item]
    if workspace != ROOT:
        args += ["--workspace", str(workspace)]
    code, output = run_step(args, 120, "建卡")
    if code != 0:
        event("建卡失败", "plan", "bad", "deepseek")
        say("建卡失败，原始输出：" + output[-400:])
        return
    match = re.search(r"(T-\d{6}-\d{6})", output)
    if not match:
        say("建卡命令没返回任务号，原始输出：" + output[-400:])
        return
    tid = match.group(1)

    # 把阶段关系与交接材料落到卡上，后面的人工阅读/下一轮"继续"都靠它
    card_path = TASKS / tid / "task.json"
    data = json.loads(card_path.read_text(encoding="utf-8"))
    data["stage"] = stage
    data["continues"] = prev["id"]
    data["workspace"] = str(workspace)
    data["unfinished_after"] = plan.get("unfinished_after") or []
    card_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (TASKS / tid / "handoff.md").write_text(
        f"# 第 {stage} 阶段交接材料（来自 {prev['id']}）\n\n"
        + context
        + "\n\n## 本阶段规划依据\n"
        + f"- 规划器输出：{json.dumps(plan, ensure_ascii=False)}\n",
        encoding="utf-8")
    (TASKS / tid / "plan.md").write_text(
        f"# 计划（第 {stage} 阶段，自动生成）\n\n## 用户指令\n{request}\n\n## 目标\n{card['goal']}\n\n"
        f"## 验收标准\n" + "\n".join(f"- {a}" for a in card["acceptance"])
        + "\n\n## 验收命令\n" + "\n".join(f"- `{v}`" for v in card["verify"])
        + "\n\n## 允许改动的路径\n" + "、".join(card["scope"])
        + f"\n\n## 工作区\n{workspace}\n\n## 上一阶段\n{prev['id']}\n",
        encoding="utf-8")

    event(f"建了第 {stage} 阶段任务卡 {tid}：{card['title']}", "plan", "ok", "deepseek", tid)
    pipeline(tid, card, request)
    publish_executor_voice(tid)
    publish_reviewer_voice(tid)



# ---------------- 群聊式大脑 ----------------
PROJECT_FILE = ROOM / "project.json"


def load_project() -> dict:
    try:
        return json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_project(data: dict) -> None:
    ROOM.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    PROJECT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


SPEAKER = {"user": "用户", "deepseek": "DeepSeek(大脑)", "codex": "Codex(执行)",
           "reviewer": "Codex复核", "system": "系统"}


def thread_text(limit: int = 40, skip_events: bool = True) -> str:
    """把群里的对话按人话拼出来，让大脑看得到别人说了什么。"""
    lines: list[str] = []
    for msg in room.read_all()[-limit:]:
        if skip_events and msg.get("kind") == "event":
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        who = SPEAKER.get(msg.get("from"), msg.get("from"))
        lines.append(f"{who}: {text[:1500]}")
    return "\n".join(lines)


BRAIN_PROMPT = """你是这个协作群的"大脑"，名字叫 DeepSeek。群里还有用户、Codex(执行者)、Codex复核。
你要像在群里聊天一样回应用户，并根据需要决定是否让 Codex 去做事。
用户的需求常常是模糊的，这很正常——你要用群里的上下文和你记得的项目信息去理解他。

## 群里最近的对话
{thread}

## 当前项目设置
{project}

## 用户刚说的话
{request}

## 你可以做的事（选一个 action）
- "chat"：只是回话/提问。当用户还没说清要做什么、或者你在跟他确认时用。
- "set_project"：用户明确了工作区或项目名。填 workspace（必须是他给出的绝对路径）和 project_name。
- "run"：要 Codex 动手改代码。填 task（标题/目标/验收标准/验收命令/允许改动路径）。
- "continue"：用户说继续/下一步，接着上一阶段做。

## 输出（严格 JSON，不要解释、不要 Markdown 代码块）
回话/提问时：
{{"say": "你在群里说的话（中文，像人一样，别长篇大论）", "action": "chat", "workspace": "", "project_name": "", "task": null}}

让 Codex 动手时（task 必须是**对象**，绝不能是一段话）：
{{"say": "说明我的理解和接下来做什么", "action": "run", "workspace": "E:\\\\某个仓库", "project_name": "", "task": {{"title": "不超过30字", "goal": "做到什么程度", "acceptance": ["可判真假的标准"], "verify": ["可直接执行的命令"], "scope": ["相对路径"]}}}}

## 规则
- say 永远是必填的：用户看得到这句话。要具体，不要"好的我明白了"这种空话。
- 需求模糊但有项目上下文：按最合理的理解动手，并在 say 里一句话说明你的假设，别追问一堆。
- 连项目都还没定：action="chat"，直接问一句"工作区是哪个目录"。
- action="run" 时 task 必须有可判真假的 acceptance 和能直接执行的 verify（优先 `python -m unittest discover -s tests -t . -v`）。
- 别把一整周的工作塞进一次 task；一次做能扎实做完的一小块。
- 只输出 JSON。"""


def brain_decide(request: str, workspace: Path | None = None) -> dict | None:
    project = load_project()
    prompt = (BRAIN_PROMPT
              .replace("{thread}", thread_text() or "（还没有对话）")
              .replace("{project}", json.dumps(project, ensure_ascii=False) if project else "（还没设置）")
              .replace("{request}", request))
    ws = str(workspace or project.get("workspace") or ROOT)
    out = ROOM / "planner"
    out.mkdir(parents=True, exist_ok=True)
    result = run_codex(prompt, workspace=ws, out_prefix=str(out / f"brain-{int(time.time())}"),
                       sandbox="read-only", timeout=300, model="gpt-6.1-terra",
                       reasoning_effort="low")
    text = ""
    if result.get("last"):
        text = Path(result["last"]).read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        log(f"大脑没给出 JSON：{text[:300]}")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        log(f"大脑 JSON 解析失败：{exc}")
        return None


def post_agent(agent: str, tid: str | None, ok: bool, content: str = "",
               artifacts: list[str] | None = None, error: str = "") -> None:
    """把某个 Agent 的这一步发成一条聊天消息（带证据引用，不灌日志）。"""
    arts = list(artifacts or [])
    if tid:
        for item in agents._artifact_paths(tid, agent):
            if item not in arts:
                arts.append(item)
    text = content.strip() or f"{agents.ROLE_LABELS.get(agent, agent)}完成"
    try:
        room.post(agent, text, "result",
                  {"task": tid, "artifacts": arts,
                   "status": "complete" if ok else "failed",
                   "error": error[-600:] if error else ""})
    except TypeError:            # 过渡期：老版 room.post 只认 4 个位置参数
        room.post(agent, text, "result", {"task": tid})


@contextmanager
def thinking(who: str):
    """告诉网页"当前是谁在干活"，让右栏与输入栏显示正确的角色。"""
    typing(True, who)
    try:
        yield
    finally:
        typing(False)


def publish_executor_voice(tid: str) -> None:
    """把 Codex 干完的汇报贴进群里，用户和其他 agent 都看得到。"""
    directory = TASKS / tid
    candidates = sorted(directory.glob("exec-*.last.md"))
    if not candidates:
        return
    text = _read(candidates[-1], 1500)
    if text:
        post_agent("executor", tid, True, text)


def publish_reviewer_voice(tid: str) -> None:
    directory = TASKS / tid
    candidates = sorted(directory.glob("review-*.last.md"))
    if not candidates:
        return
    text = _read(candidates[-1], 1200)
    if text:
        verdict_ok = "needs_fix" not in text.lower()
        post_agent("reviewer", tid, verdict_ok, text)


def handle_chat(request: str) -> None:
    """群聊主入口：大脑先回话，再决定要不要动手。"""
    project = load_project()
    workspace = None
    if project.get("workspace"):
        try:
            workspace = Path(project["workspace"]).resolve()
        except OSError:
            workspace = None

    event("大脑正在看群里的对话…", "plan", "run", "deepseek")
    plan = brain_decide(request, workspace)
    if plan is None:
        say("我这边规划模型没给出可用结果，这条我先用手工方式接。")
        return

    speech = (plan.get("say") or "").strip()
    if speech:
        room.post("deepseek", speech, "msg")

    action = (plan.get("action") or "chat").strip()
    if action == "run" and not isinstance(plan.get("task"), dict):
        # 模型偶尔把 task 写成一段话：不要硬跑，改成追问一句
        room.post("deepseek",
                  "我大致明白你要什么，但拆不出可执行的任务卡。你说清「改哪个文件、做到什么程度、怎么算成功」，我就动手。",
                  "msg")
        return
    if action == "set_project":
        raw_ws = (plan.get("workspace") or "").strip()
        if not raw_ws:
            room.post("deepseek", "工作区路径我没拿到，你直接给我一个目录的绝对路径就行。", "msg")
            return
        candidate = raw_ws.strip("\"'")
        if candidate.lower().startswith("file:"):
            candidate = candidate[5:]
        target, why = detect_workspace(candidate)
        if target is None:
            room.post("deepseek", f"这个路径我没法用：{why}", "msg")
            return
        data = load_project()
        data["workspace"] = str(target)
        data["name"] = (plan.get("project_name") or target.name).strip() or target.name
        save_project(data)
        event(f"项目已设置：{data['name']}（{target}）", "plan", "ok", "deepseek")
        return

    if action == "continue":
        handle_continue(request)
        return

    if action == "run":
        asked = any(mark in request for mark in ("?", "？", "吗"))
        imperative = any(word in request for word in ("帮我", "给我", "去做", "改一下", "加上", "修好", "实现", "补上"))
        if asked and not imperative:
            room.post("deepseek", "要我现在就让 Codex 动手吗？回一句「动手」或「继续」我就开工。", "msg")
            return
        task = plan.get("task") or {}
        card = sanitize_plan(task)
        target = workspace
        if plan.get("workspace"):
            resolved, why = detect_workspace(str(plan["workspace"]))
            if resolved is not None:
                target = resolved
        if target is None:
            target, why = detect_workspace(request)
        if target is None:
            room.post("deepseek", "要动手得先知道改哪个目录，给我一个仓库的绝对路径。", "msg")
            return
        run_card(card, target, request)
        return
    # action == chat：话已经说了，不动手


def _require_approval() -> bool:
    """配置里 policy.require_approval=true 时，计划出来先等用户点头。"""
    try:
        cfg = json.loads((ROOT / "config" / "team.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool((cfg.get("policy") or {}).get("require_approval"))


def run_card(card: dict, workspace: Path, request: str) -> None:
    args = ["new", "--title", card["title"], "--goal", card["goal"]]
    for item in card["acceptance"]:
        args += ["--acceptance", item]
    for cmd in card["verify"]:
        args += ["--verify", cmd]
    for item in card["scope"]:
        args += ["--scope", item]
    if workspace != ROOT:
        args += ["--workspace", str(workspace)]
    code, output = run_step(args, 120, "建卡")
    if code != 0:
        event("建卡失败", "plan", "bad", "deepseek")
        say("建卡失败，原始输出：" + output[-400:])
        return
    match = re.search(r"(T-\d{6}-\d{6})", output)
    if not match:
        say("建卡命令没返回任务号，原始输出：" + output[-400:])
        return
    tid = match.group(1)
    (TASKS / tid / "plan.md").write_text(
        f"# 计划（自动生成）\n\n## 用户原话\n{request}\n\n## 目标\n{card['goal']}\n\n"
        f"## 验收标准\n" + "\n".join(f"- {a}" for a in card["acceptance"])
        + "\n\n## 验收命令\n" + "\n".join(f"- `{v}`" for v in card["verify"])
        + "\n\n## 允许改动的路径\n" + "、".join(card["scope"])
        + f"\n\n## 工作区\n{workspace}\n", encoding="utf-8")
    event(f"建了任务卡 {tid}：{card['title']}", "plan", "ok", "deepseek", tid)
    post_agent("planner", tid, True,
               f"需求已拆好：{len(card['acceptance'])} 条验收标准，"
               f"{len(card['scope'])} 个允许改动的路径，计划写进了 plan.md。")
    if _require_approval():
        try:
            control.wait_approval(tid, on_event=lambda ev, st: control_note(ev, tid, st),
                                  heartbeat=beat)
        except control.TaskStopped:
            control_note("stopped", tid)
            post_agent("coordinator", tid, False, "已按你的要求停下，这一轮还没开始执行。")
            return
    pipeline(tid, card, request)


def pipeline(tid: str, card: dict, request: str) -> None:
    """执行 → 验收 → 复核 → 报告 → 交接记录。每一步都单独发一条 Agent 消息。"""
    try:
        _pipeline(tid, card, request)
    except control.TaskStopped:
        control_note("stopped", tid)
        post_agent("coordinator", tid, False, "已按你的要求停止，这轮不算完成。")
        card_path = TASKS / tid / "task.json"
        try:
            task = json.loads(card_path.read_text(encoding="utf-8"))
            task["status"] = "stopped"
            task["outcome"] = "failure"
            task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            card_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass
        do_report(tid)
        mark_finished(tid)


def _pipeline(tid: str, card: dict, request: str) -> None:
    for round_no in range(1, 3):
        with thinking("executor"):
            code, _ = step(tid, ["dispatch", "-t", tid], 1200,
                           f"第 {round_no} 轮执行", "dispatch")
        exec_log = TASKS / tid / f"exec-{round_no:02d}.last.md"
        post_agent("executor", tid, code == 0,
                   _read(exec_log, 900) if exec_log.exists() else "",
                   error="" if code == 0 else f"执行退出码 {code}")
        if code != 0:
            event(f"第 {round_no} 轮执行未正常结束", "dispatch", "bad", "codex", tid)
        with thinking("tester"):
            code, _ = step(tid, ["verify", "-t", tid], 600,
                           f"第 {round_no} 轮验收", "verify")
        post_agent("tester", tid, code == 0,
                   f"第 {round_no} 轮验收{'通过' if code == 0 else '未通过'}")
        if code == 0:
            break
        if round_no == 1:
            (TASKS / tid / "verdict-01.md").write_text(
                "# 验收未通过（自动返工）\n\n## 结论\nrepair\n\n## 要求\n"
                "上一条 verify-01.txt 里失败的用例必须修好；不要改动验收标准、不要删除测试。\n",
                encoding="utf-8")
            event(f"{tid} 验收未通过，自动返工一轮", "verify", "bad", "deepseek", tid)
        else:
            event(f"{tid} 两轮都没过，停下等人", "verify", "bad", "deepseek", tid)
            say(f"任务 {tid} 两轮验收都没通过，已停下等你拍板。看 tasks/{tid}/verify-*.txt",
                task_id=tid, status="failed")
            do_report(tid)
            mark_finished(tid)
            append_progress(tid)
            return

    for attempt in range(1, 3):
        with thinking("reviewer"):
            code, _ = step(tid, ["review", "-t", tid], 900,
                           f"第 {attempt} 次复核", "review")
        if code == 0:
            post_agent("reviewer", tid, True, "复核通过")
            break
        post_agent("reviewer", tid, False, "复核要求修改")
        if attempt == 2:
            event(f"{tid} 复核两次都未通过，停下等人", "review", "bad", "reviewer", tid)
            break
        findings = latest_review_text(tid)
        current = json.loads((TASKS / tid / "task.json").read_text(encoding="utf-8"))
        next_round = len(current.get("rounds") or []) + 1
        write_repair_verdict(tid, next_round, findings)
        current["repair_pending"] = True
        (TASKS / tid / "task.json").write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        event(f"{tid} 复核挑出问题，自动返工第 {next_round} 轮", "review", "bad", "reviewer", tid)
        with thinking("executor"):
            step(tid, ["dispatch", "-t", tid], 1200,
                     f"第 {next_round} 轮返工", "dispatch")
        with thinking("tester"):
            vcode, _ = step(tid, ["verify", "-t", tid], 600,
                                f"第 {next_round} 轮验收", "verify")
        post_agent("executor", tid, vcode == 0, f"按复核意见返工第 {next_round} 轮")
        post_agent("tester", tid, vcode == 0, f"返工后验收{'通过' if vcode == 0 else '未通过'}")
        if vcode != 0:
            event(f"{tid} 返工后验收仍未通过，停下等人", "verify", "bad", "deepseek", tid)
            break

    do_report(tid)
    mark_finished(tid)

    task = json.loads((TASKS / tid / "task.json").read_text(encoding="utf-8"))
    rounds = len(task.get("rounds") or [])
    last = (task.get("rounds") or [{}])[-1]
    reviews = task.get("reviews") or []
    verdict = (reviews[-1].get("verdict") if reviews else "未复核")
    tokens = last.get("tokens")
    say(
        f"任务 {tid} 走完了：{card['title']}\n"
        f"· 执行 {rounds} 轮，最后一轮 {last.get('seconds', '?')}s"
        f"{('，' + format(tokens, ',') + ' tokens') if tokens else ''}\n"
        f"· 验收：{'通过' if task.get('status') == 'passed' else '未通过'}\n"
        f"· 复核：{verdict}\n"
        f"· 账本：tasks/{tid}/report.md"
    )
    append_progress(tid)
    event(f"{tid} 全流程结束（{task.get('status')} / 复核 {verdict}）",
          "done", "ok" if task.get("status") == "passed" else "bad", "deepseek", tid)


def handle(request: str) -> None:
    """一条用户消息进来：先判断是不是"继续"，其余全交给群聊大脑。"""
    if is_continue_intent(request):
        handle_continue(request)
        return
    handle_chat(request)


def main() -> int:
    ap = argparse.ArgumentParser(description="常驻工作流引擎")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（调试用）")
    args = ap.parse_args()

    if not take_lock():
        print("已有另一个 worker 在跑，退出。")
        return 0
    log(f"worker 启动 pid={os.getpid()} interval={args.interval}s")
    event("工作流引擎已上线：发任务后 3 秒内自动开工", "worker", "ok", "deepseek")

    while True:
        try:
            beat()
            waiting = room.pending()
            for msg in waiting:
                request = (msg.get("text") or "").strip()
                # 先认领再干活：ack 之后大脑心跳不会重复处理同一条
                state = room._read_state()  # noqa: SLF001
                state["last_handled_id"] = max(int(state.get("last_handled_id") or 0), msg["id"])
                room._write_state(state)  # noqa: SLF001
                log(f"认领 #{msg['id']}：{request[:60]}")
                typing(True)
                try:
                    handle(request)
                except subprocess.TimeoutExpired:
                    event("执行超时，已中止这条", "dispatch", "bad", "codex")
                    say(f"任务执行超时被中止：{request[:120]}", status="failed")
                except Exception as exc:  # noqa: BLE001
                    log(f"处理失败：{type(exc).__name__}: {exc}")
                    event(f"处理异常：{type(exc).__name__}", "done", "bad", "deepseek")
                    say(f"这条我处理时出错了（{type(exc).__name__}: {exc}），原始需求：{request[:160]}",
                        status="failed")
                finally:
                    typing(False)
                # 干活期间又来了新需求：明确告诉用户"排上了"，别让人以为漏掉
                try:
                    rest = [m for m in room.pending() if int(m.get("id") or 0) > int(msg.get("id") or 0)]
                except Exception:  # noqa: BLE001
                    rest = []
                if rest:
                    preview = "；".join((m.get("text") or "").strip()[:40] for m in rest[:3])
                    say(f"收到 {len(rest)} 条新需求，已排进队列，等手上这条结束就办：{preview}")
        except Exception as exc:  # noqa: BLE001
            log(f"主循环异常：{type(exc).__name__}: {exc}")

        if args.once:
            return 0
        if NUDGE.exists():
            NUDGE.unlink(missing_ok=True)
            continue                      # 有新消息，立刻回到循环顶部处理
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())