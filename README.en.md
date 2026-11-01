# agent-team · a team of coding agents in one group chat

[中文说明](README.md) · [Install](docs/INSTALL.md) · [Architecture](docs/ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md)

[![CI](https://github.com/Lucusmaster/Cowork_with_agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Lucusmaster/Cowork_with_agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Coding agents that actually talk to each other. **DeepSeek plans**, **Codex executes**, **a read-only
Codex reviews** — and you talk to all of them in one local group chat instead of copy-pasting between
two tabs.

```text
you ──▶ chat UI ──▶ brain (reads the whole thread, replies, writes a task card)
                        └──▶ Codex executor (edits code, runs your acceptance command)
                                └──▶ read-only reviewer (PASS / NEEDS_FIX)
                                        └──▶ report posted back into the chat
```

## Why

Two-agent workarounds usually fail at the same place: the human becomes the message bus. You paste a
report to one agent, get guidance, paste it back to the other, repeat. This project removes that step —
every agent's output lands in the same message log, so each one sees what the others said.

## What it does

- **Group chat, not a task form.** Say what you want in plain language — vague is fine. The brain reads
  the thread plus the bound project, replies, and decides: chat, bind project, run a task, or continue
  the previous stage.
- **Workspace binding.** "The workspace is `E:\my-project`" — it walks up to the nearest `.git` root,
  refuses ambiguous or missing paths instead of guessing.
- **Evidence-only acceptance.** A card must declare falsifiable criteria and a runnable verification
  command; exit codes, raw output and `git diff` decide — not the executor's own claim.
- **Independent review.** A different, read-only model inspects the diff for fake passes and
  out-of-scope edits; `NEEDS_FIX` triggers an automatic rework round.
- **Stage-by-stage progress.** Say "continue" and the next card is built from the previous report,
  raw failing cases, review findings and the workspace state.
- **Honest lifecycle.** Closing the chat page stops the engine — after any in-flight task reaches a
  terminal state.

## The chat workbench

The UI is a conversation, not a form: workspaces and past sessions on the left, the message stream in the middle, agent status / current task / test output / diff on the right. You type plain language; you never touch task IDs or paste results between agents.

Five roles post into the same session: **coordinator** (understands the request and summarises), **planner** (turns it into an acceptance-checked task card), **executor** (edits code and runs tests), **tester** (runs the acceptance commands outside the sandbox), **reviewer** (a different model reads the diff, read-only).

Messages carry **references, not logs**:

```json
{"agent": "executor", "task_id": "T-001", "content": "Implemented, tests pass",
 "artifacts": ["tasks/T-001/exec-01.last.md", "workspace:src/login.py"],
 "status": "complete"}
```

Failures are failures: a non-zero acceptance command, an out-of-scope edit, or a `NEEDS_FIX` review marks the message red with the reason and records `outcome=failure` — the flow finishing never fakes success.
## Quick start after cloning

Read the [installation guide](docs/INSTALL.md) first. It lists the required tools, platform-specific setup, optional development dependencies, and doctor output.

Requires Python ≥ 3.10, git, and a logged-in Codex CLI (`codex login`). Node.js is only needed for JavaScript/TypeScript workspaces. Pillow is only needed for development and icon generation.

```powershell
python -m agent_team.cli doctor        # check environment
python -m agent_team.cli start         # start workbench + engine
# open http://127.0.0.1:8899
# first message: "The workspace is E:\my-project, call this project my-project"
```

Or install the command:

```bash
pip install -e .
agent-team start
```

## Status

Windows is the platform this is actually exercised on (PowerShell 5.1, GBK consoles, detached
no-console processes). The code has POSIX branches and CI runs on Ubuntu too, but macOS/Linux have
not been battle-tested.

## Safety

The executor **edits real files and runs real commands** in the workspace you bind. Defaults are
`sandbox=workspace-write` for execution and `read-only` for review. Read [SECURITY.md](SECURITY.md)
before pointing it at anything you care about.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — roles, message bus, state machine, guards, known limits
- [docs/ROADMAP.md](docs/ROADMAP.md) — what's done, what's next, what's explicitly out of scope
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev loop and hard rules
- [CHANGELOG.md](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE).
