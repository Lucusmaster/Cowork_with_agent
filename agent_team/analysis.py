"""规划阶段的并行只读分析。

一个需求进来，除了"大脑"给计划，还需要两件事：
- 复核者预判风险（这个仓库里最容易踩的坑、必须守住的边界）；
- 验收者给验收口径（该跑什么命令、该断言什么）。

这两件事互不依赖，以前要排队做，现在同时跑：墙钟时间取两者的最大值，
不是两者相加。两个都是**只读**沙箱，不改文件、不改工作区。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .codex_bridge import run_codex

RISK_PROMPT = """你在一个多 Agent 协作群里担任复核者。现在还在规划阶段，你只做只读分析。

需求：{request}
工作区：{ws}

请直接用中文回答，不要动任何文件，不要写代码，控制在 12 行以内：
1) 这个改动最容易破坏的现有行为是什么（具体到文件/函数）
2) 必须覆盖的边界情况
3) 明确不该碰的地方
"""

VERIFY_PROMPT = """你在一个多 Agent 协作群里担任验收者。现在还在规划阶段，你只做只读分析。

需求：{request}
工作区：{ws}
工作区现状（顶层）：{facts}

请直接用中文回答，不要动任何文件，控制在 10 行以内：
1) 一条可以直接在这个工作区里执行的验收命令，用来检查「这次任务的产出」（退出码说话）：
   - 产出是代码、或者工作区本来就有测试：用 `python -m unittest discover -s tests -t . -v`（或 `python -m pytest`）；
   - 产出是 HTML / 文档 / 配置 / 脚本这类文件：用能直接检查产物的命令，例如
     `python -c "..."` 断言文件存在、能解析、含关键内容——不要假设工作区里有 tests/ 目录；
   - 命令里引用的路径必须现在就有，或者是任务允许新建的；用户明确说"不需要测试"时不要造测试目录；
2) 3~5 条必须断言到的行为（可判真假，不写"代码质量良好"这种空话）
"""


def workspace_facts(ws: Path) -> str:
    """给验收者/规划者看的现场：顶层有什么、有没有 tests/。

    之前只把路径丢给模型，它在空目录/非 Python 项目里也会照"优先 unittest"的提示
    编出 `discover -s tests`，验收命令根本跑不起来。
    """
    try:
        entries = sorted(ws.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return "（读不到这个目录）"
    names = [p.name + ("/" if p.is_dir() else "") for p in entries[:12]]
    more = f" 等 {len(entries)} 项" if len(entries) > 12 else ""
    bits: list[str] = []
    if (ws / ".git").exists():
        bits.append("是 git 仓库")
    if (ws / "tests").is_dir():
        bits.append("已有 tests/")
    py = sum(1 for p in entries if p.suffix == ".py")
    if py:
        bits.append(f"顶层 {py} 个 .py")
    if not bits:
        bits.append("没有测试目录")
    return f"{'、'.join(names) if names else '空目录'}{more}（{'；'.join(bits)}）"

BOTH = {"risk": RISK_PROMPT, "verify": VERIFY_PROMPT}


def _one(kind: str, prompt: str, workspace: Path, out_dir: Path, model: str,
         effort: str, timeout: int) -> dict:
    started = time.time()
    try:
        result = run_codex(
            prompt, workspace=str(workspace),
            out_prefix=str(out_dir / f"analysis-{kind}"),
            sandbox="read-only", timeout=timeout, model=model, reasoning_effort=effort)
    except Exception as exc:  # noqa: BLE001  一个分析挂了不能连累另一个
        return {"ok": False, "kind": kind, "text": "", "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - started, 1)}
    text = ""
    if result.get("last"):
        try:
            text = Path(result["last"]).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
    if not text:
        text = str(result.get("text") or "").strip()
    ok = bool(result.get("ok", True)) and bool(text)
    return {"ok": ok, "kind": kind, "text": text,
            "error": "" if ok else str(result.get("error") or "没有输出"),
            "seconds": round(time.time() - started, 1)}


def analyze(request: str, workspace: Path, out_dir: Path, *,
            risk_model: str = "", risk_effort: str = "low",
            verify_model: str = "", verify_effort: str = "low",
            timeout: int = 240) -> dict:
    """两份只读分析同时跑，返回 {"risk": {...}, "verify": {...}, "wall": 秒}。

    任何一份失败都不影响另一份，也不会抛异常——规划阶段不能因为预审失败就卡住。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    jobs = {
        "risk": (RISK_PROMPT.format(request=request, ws=workspace), risk_model, risk_effort),
        "verify": (VERIFY_PROMPT.format(request=request, ws=workspace,
                                        facts=workspace_facts(workspace)),
                   verify_model, verify_effort),
    }
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="analysis") as pool:
        futures = {
            kind: pool.submit(_one, kind, prompt, workspace, out_dir, model, effort, timeout)
            for kind, (prompt, model, effort) in jobs.items()
        }
        for kind, fut in futures.items():
            try:
                results[kind] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[kind] = {"ok": False, "kind": kind, "text": "", "seconds": 0.0,
                                 "error": f"{type(exc).__name__}: {exc}"}
    serial = round(sum(item.get("seconds") or 0 for item in results.values()), 1)
    return {
        **results,
        "wall": round(time.time() - started, 1),
        "serial": serial,
        "saved": round(serial - (time.time() - started), 1),
    }


def plan_notes(result: dict) -> tuple[str, str]:
    """把两份分析整理成 planner 消息用的一小段（没有就返回空串）。"""
    lines: list[str] = []
    risk = (result.get("risk") or {}).get("text") or ""
    verify = (result.get("verify") or {}).get("text") or ""
    if risk:
        lines.append("预审风险：\n" + _trim(risk, 600))
    if verify:
        lines.append("验收口径：\n" + _trim(verify, 500))
    return "\n\n".join(lines), _trim(risk, 4000)


def _trim(text: str, limit: int) -> str:
    text = "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"