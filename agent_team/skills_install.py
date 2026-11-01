"""把仓库里的「正本 skill」装到各家 agent 的加载位置。

为什么要这一步：协作台是个**项目**，但用它的 agent 各有各的加载方式——DSH 读
`~/.dsh/skills/<名字>/SKILL.md`，Codex CLI 读 `~/.codex/skills/<名字>/SKILL.md`，
Claude Code 读 `~/.claude/skills/<名字>/SKILL.md`（三处格式相同：YAML frontmatter + 正文）。
没有 skill 加载机制的客户端（豆包等）走 MCP，配置在 docs/MCP.md。

正本只有一份：仓库里的 `skills/agent-team-collab/SKILL.md`。它跟着版本走、能被测试验证
（`tests/test_skills.py` 会把里面提到的每条命令拿去和真实 CLI 比对，防止文档漂移）。

安装策略（不希望把用户手改的东西覆盖掉）：
- 目标位置没有文件 → 直接写；
- 有文件但内容与正本一致 → 跳过（幂等）；
- 有文件但内容不同 → 先备份成 `SKILL.md.bak-<时间>`，再写；`--force` 才不备份覆盖。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from .paths import workbench_root

SKILL_NAME = "agent-team-collab"

# 各客户端的用户级 skills 目录（相对 home）
TARGETS = {
    "dsh": Path(".dsh") / "skills",
    "codex": Path(".codex") / "skills",
    "claude": Path(".claude") / "skills",
}


def source_skill() -> Path:
    """正本：仓库里的 skills/agent-team-collab/SKILL.md。"""
    return workbench_root() / "skills" / SKILL_NAME / "SKILL.md"


def target_dir(target: str, home: Path | None = None) -> Path:
    home = home or Path.home()
    base = TARGETS.get(target)
    if base is None:
        raise ValueError(f"不认识的目标：{target}（可选：{', '.join(TARGETS)}）")
    return home / base / SKILL_NAME


def installed_version(path: Path) -> str:
    """读 frontmatter 里的 version；没有就返回空串。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() == "version":
            return value.strip().strip('"').strip("'")
    return ""


def install(target: str, home: Path | None = None, force: bool = False,
            dry_run: bool = False) -> dict:
    """装一份，返回结果（ok/message/action）。动作：created / updated / same / skipped。"""
    src = source_skill()
    if not src.is_file():
        return {"target": target, "ok": False,
                "action": "error", "message": f"正本不存在：{src}"}
    body = src.read_text(encoding="utf-8")
    dest_dir = target_dir(target, home)
    dest = dest_dir / "SKILL.md"

    if dest.is_file():
        try:
            current = dest.read_text(encoding="utf-8")
        except OSError as exc:
            return {"target": target, "ok": False, "action": "error",
                    "message": f"读不了已有文件：{exc}"}
        if current == body:
            return {"target": target, "ok": True, "action": "same", "path": str(dest),
                    "message": "已经是最新，跳过"}
        if dry_run:
            return {"target": target, "ok": True, "action": "would-update", "path": str(dest),
                    "message": "会覆盖（已有内容不同）"}
        backup = ""
        if not force:
            # 不 --force 就先备份：用户可能手改过
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup_path = dest_dir / f"SKILL.md.bak-{stamp}"
            try:
                shutil.copy2(dest, backup_path)
                backup = str(backup_path)
            except OSError as exc:
                return {"target": target, "ok": False, "action": "error",
                        "message": f"备份失败，不敢覆盖：{exc}"}
        if dry_run:
            return {"target": target, "ok": True, "action": "would-update", "path": str(dest)}
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        return {"target": target, "ok": True, "action": "updated", "path": str(dest),
                "backup": backup, "message": "已更新（旧文件已备份）"}

    if dry_run:
        return {"target": target, "ok": True, "action": "would-create", "path": str(dest)}
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    return {"target": target, "ok": True, "action": "created", "path": str(dest),
            "message": "已安装"}


def install_all(targets: list[str] | None = None, home: Path | None = None,
                force: bool = False, dry_run: bool = False) -> list[dict]:
    results = []
    for target in (targets or list(TARGETS)):
        results.append(install(target, home=home, force=force, dry_run=dry_run))
    return results


def mcp_snippet(detected: list[str] | None = None) -> str:
    """给没有 skill 机制的客户端（豆包等）准备的 MCP 配置片段。"""
    # 别自己再转义：json.dumps 会把反斜杠处理掉，预先 replace 会变成四重斜杠
    payload = {"mcpServers": {"agent-team": {
        "command": "python",
        "args": ["-m", "agent_team.mcp_server"],
        "cwd": str(workbench_root())}}}
    note = []
    if detected:
        note.append("检测到的客户端：" + "、".join(detected))
    note.append("没有 skill 加载机制的客户端（豆包等）：把这段贴进它们的 MCP 配置即可")
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n\n（" + "；".join(note) + "）"


def detect(home: Path | None = None) -> list[str]:
    """看这台机器上哪些客户端的目录真的存在。"""
    home = home or Path.home()
    found = []
    for name, base in TARGETS.items():
        if (home / base).parent.exists():
            found.append(name)
    return found


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="agent-team install-skills",
        description="把协作台的使用 skill 装到 DSH / Codex / Claude Code 的 skills 目录")
    ap.add_argument("--targets", default="", help="逗号分隔：dsh,codex,claude（默认全装）")
    ap.add_argument("--home", default="", help="换一个 home 目录（测试用）")
    ap.add_argument("--force", action="store_true", help="已有文件直接覆盖，不备份")
    ap.add_argument("--dry-run", action="store_true", help="只说会做什么，不动文件")
    ap.add_argument("--mcp", action="store_true", help="只打印 MCP 配置片段")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    home = Path(args.home) if args.home else None
    if args.mcp:
        print(mcp_snippet(detect(home)))
        return 0

    targets = [t.strip() for t in args.targets.split(",") if t.strip()] or None
    results = install_all(targets, home=home, force=args.force, dry_run=args.dry_run)
    ok = True
    for item in results:
        mark = {"created": "＋", "updated": "↻", "same": "·", "would-create": "?",
                "would-update": "?", "error": "✗"}.get(item.get("action"), "·")
        print(f"{mark} {item['target']:6} {item.get('message') or item.get('action')}"
              f"{'  ← ' + item['path'] if item.get('path') else ''}")
        ok &= bool(item.get("ok"))
    if not args.dry_run and not any(
            item.get("action") in ("created", "updated") for item in results):
        print("（都装好了；没有 skill 机制的客户端用 `install-skills --mcp` 拿配置）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())