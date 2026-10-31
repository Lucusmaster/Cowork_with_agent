"""统一 Agent 适配层。

所有角色都通过 `run(agent_role, session_context, workspace, task)` 调用，
上层（协调器 / 网页）不需要知道底下是 Codex、DeepSeek 还是别的 CLI。

当前实现：planner / executor / tester / reviewer 都落到 `python -m agent_team.team`
（任务卡协议内核），coordinator 由 `worker.py` 自己用大脑模型完成。
换模型只改 `config/team.json`；换执行器只改 `codex_bridge.py`。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .winproc import quiet_flags

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "team.json"

ROLE_LABELS = {
    "coordinator": "协调",
    "planner": "规划",
    "executor": "执行",
    "tester": "验收",
    "reviewer": "复核",
}

ROLE_ORDER = ["coordinator", "planner", "executor", "tester", "reviewer"]

# 每个角色对应的 team.py 子命令（None 表示由协调器在本进程内完成）
ROLE_COMMAND = {
    "executor": "dispatch",
    "tester": "verify",
    "reviewer": "review",
}


@dataclass
class AgentResult:
    agent: str
    ok: bool
    content: str
    artifacts: list[str] = field(default_factory=list)
    raw: str = ""
    error: str = ""

    def as_message(self, task_id: str | None = None) -> dict:
        return {
            "agent": self.agent,
            "task_id": task_id,
            "content": self.content,
            "artifacts": list(self.artifacts),
            "status": "complete" if self.ok else "failed",
        }


def _load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def available() -> list[dict]:
    """配置里声明的 Agent 列表；没配就给出默认角色。"""
    cfg = _load_config().get("agents")
    if isinstance(cfg, list) and cfg:
        out = []
        for item in cfg:
            role = str(item.get("role") or "").strip()
            if not role:
                continue
            out.append({
                "role": role,
                "name": item.get("name") or ROLE_LABELS.get(role, role),
                "model": item.get("model") or "",
                "effort": item.get("effort") or "",
                "sandbox": item.get("sandbox") or "",
                "enabled": bool(item.get("enabled", True)),
            })
        if out:
            return out
    return [{"role": role, "name": ROLE_LABELS[role], "model": "", "effort": "",
             "sandbox": "", "enabled": True} for role in ROLE_ORDER]


def _read(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _latest(directory: Path, pattern: str) -> Path | None:
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def _artifact_paths(tid: str, role: str) -> list[str]:
    """把这一步留下的证据文件变成消息里的 artifact 引用（仓库相对路径）。"""
    directory = ROOT / "tasks" / tid
    if not directory.is_dir():
        return []
    patterns = {
        "planner": ["plan.md", "prompt-*.md"],
        "executor": ["exec-*.last.md"],
        "tester": ["verify-*.txt"],
        "reviewer": ["review-*.last.md", "verdict-*.md"],
    }.get(role, [])
    out: list[str] = []
    for pattern in patterns:
        latest = _latest(directory, pattern)
        if latest is not None:
            out.append(str(latest.relative_to(ROOT)).replace("\\", "/"))
    return out


def _call_team(command: str, tid: str, timeout: int) -> tuple[int, str]:
    """调用任务卡内核；不弹黑框，输出统一 UTF-8。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_team.team", command, "-t", tid],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=quiet_flags(),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"{command} 超时（{timeout}s）"
    except OSError as exc:  # 解释器都起不来
        return 125, f"{command} 启动失败：{exc}"


def _summarize(role: str, tid: str, ok: bool, output: str) -> str:
    """给用户看的一句话结论，正文留在 artifact 里，不往聊天里灌日志。"""
    directory = ROOT / "tasks" / tid
    if role == "executor":
        text = _read(_latest(directory, "exec-*.last.md") or directory / "none", 1200)
        head = text.strip().splitlines()
        brief = " ".join(line.strip("# ").strip() for line in head[:6] if line.strip())[:280]
        return f"改动完成。{brief}" if ok else f"执行没跑通：{output.strip()[-280:]}"
    if role == "tester":
        verify = _read(_latest(directory, "verify-*.txt") or directory / "none", 1200)
        tail = " ".join(line.strip() for line in verify.strip().splitlines()[-4:])[:280]
        return f"验收{'通过' if ok else '未通过'}：{tail}"
    if role == "reviewer":
        review = _read(_latest(directory, "review-*.last.md") or directory / "none", 1200)
        first = next((line.strip() for line in review.splitlines() if line.strip()), "")
        return f"复核{'通过' if ok else '要求修改'}：{first[:200]}"
    return output.strip()[:280]


def run(agent_role: str, session_context: str, workspace: Path, task: dict | None) -> AgentResult:
    """统一入口：一个角色干一件事，返回结构化结果。

    - executor / tester / reviewer：落到 `agent_team.team` 的 dispatch / verify / review；
    - planner：由协调器用大脑模型完成，这里只回传任务卡里的计划文件；
    - coordinator：由 `worker.py` 处理，这里不会调用。
    """
    role = (agent_role or "").strip()
    if role in ("", "coordinator"):
        return AgentResult(agent="coordinator", ok=False, content="",
                           error="coordinator 由 worker 驱动，不走适配层")
    if not task or not task.get("id"):
        return AgentResult(agent=role, ok=False, content="", error="缺少任务卡")
    tid = str(task["id"])
    if role == "planner":
        artifacts = _artifact_paths(tid, "planner")
        return AgentResult(agent="planner", ok=bool(artifacts), artifacts=artifacts,
                           content="计划已写入任务卡：" + ("、".join(artifacts) or "（无）"))
    command = ROLE_COMMAND.get(role)
    if not command:
        return AgentResult(agent=role, ok=False, content="", error=f"未知角色：{role}")

    timeout = {"executor": 1800, "tester": 900, "reviewer": 1200}.get(role, 900)
    code, output = _call_team(command, tid, timeout)
    ok = code == 0
    artifacts = _artifact_paths(tid, role)
    # executor 额外带上改动的文件，用户在右侧面板能直接看到
    if role == "executor" and workspace:
        for name in _changed_files(workspace):
            artifacts.append(f"workspace:{name}")
    return AgentResult(agent=role, ok=ok, content=_summarize(role, tid, ok, output),
                       artifacts=artifacts, raw=output, error="" if ok else output[-800:])


def _changed_files(workspace: Path) -> list[str]:
    """工作区里相对仓库根的改动文件（最多 40 个）。"""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(workspace), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=quiet_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        name = line[3:].strip().strip('"')
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        out.append(name)
        if len(out) >= 40:
            break
    return out