# 架构

协作台不是提示词，是一个跑在本机的多 Agent 系统：一个网页群聊、一个引擎进程、一个共享账本，
外面挂着 Codex CLI 作为执行者和复核者。

## 角色

| 角色 | 谁 | 职责 | 权限 |
| --- | --- | --- | --- |
| 用户 | 人 | 提需求（可以模糊）、拍板、看报告 | 全部 |
| 大脑 | DeepSeek 规划模型 | 读群聊上下文，回话、拆卡、判验收、决定下一步 | 只读 + 建卡 |
| 执行 | Codex（`gpt-6.1-terra`，effort low） | 改代码、跑验收命令 | workspace-write |
| 复核 | Codex（`gpt-6-luna`，只读） | 只看 diff 和证据，判 PASS / NEEDS_FIX | read-only |
| 调度 | `worker.py` 引擎 | 认领消息、跑流水线、写状态 | 写账本 |

## 运行时拓扑

```text
浏览器  ──HTTP──▶  room_web (8899)  ──写──▶  room/messages.jsonl  ◀──读/写── worker 引擎
                        │                        │                              │
                        └── 发消息时写 nudge ─────┘                              ├─▶ codex exec（执行）
                                                                                └─▶ codex exec（复核）
账本：room/（消息、状态、锁、日志）  任务卡：tasks/<ID>/   业务产物：任务卡里的 workspace
```

- **消息即总线**：所有人（用户、大脑、Codex、复核者）都往 `room/messages.jsonl` 写，带 `from`/`kind`。
  谁都能读到别人说了什么——这是省掉"两个 agent 之间复制粘贴"的机制。
- **立刻响应**：网页发消息时顺手写 `room/nudge`，引擎 0.5 秒轮询看到它就立刻处理，而不是干等 3 秒。
- **看得见在想**：处理期间 `room/typing.json` 亮着，页面显示"正在思考…"，结束自动熄灭。

## 任务卡与状态机

`tasks/<ID>/task.json` 是唯一事实来源，一个任务一张卡：

```text
planned ──dispatch──▶ dispatched ──exec──▶ executed ──verify──▶ passed / failed
                                                  └──review──▶ reviews[]（PASS / NEEDS_FIX）
passed/failed ──report──▶ report.md ──▶ finished_at（真正的终点）
```

- `finished_at` / `report.md` 才算收工；只有 `passed` 不算，否则验收和报告会被跳过。
- 返工由复核驱动：复核 NEEDS_FIX → 写 `verdict-<round>.md` → 置 `repair_pending` → 重新派发。
- 一次性证据：`exec-<round>.last.md`、`verify-<round>.txt`、`review-<round>.last.md`、`report.md`。

## 验收协议

一个任务卡必须能回答"怎么才算做完"，否则大脑不许建卡：

- `acceptance`：可判真假的标准（不是"优化一下"）；
- `verify`：可直接执行的命令，按任务产出选型：代码/测试用 `python -m unittest ...` 或 `python -m pytest ...`，
  静态 HTML/文档/配置用 `python -c "..."` 直接检查产物（存在、能解析、含关键内容），不要假设有 `tests/`；
- 判分只看证据：验收命令退出码、输出原文、`git diff`——不看执行者自己的说法。

## 守卫

| 守卫 | 防的事 |
| --- | --- |
| 工作区绑定 `detect_workspace` | 任务卡绑错目录，产物写进账本仓库；无路径/多路径/路径不存在一律拒绝并追问；普通无 Git 文件夹也允许作为新项目 |
| `ledger_baseline` | 执行时把账本（本仓库）改脏；验收只报新增/变更 |
| `workspace_baseline` + `baseline.diff` | 复核把"任务开始前就有的脏改动"算到本轮头上，误判越界 |
| `quiet_flags`（CREATE_NO_WINDOW） | 后台每次调 codex/git/python 闪一个黑框 |
| 意图判定 | "继续聊两句：" 这类闲聊被当成阶段推进，白跑整条流水线 |
| 问句不开工 | 你在问"这样行不行"，它却直接动手 |

## 分阶段推进

大项目一阶段一卡：

- 卡上记 `stage`、`continues`（接哪张卡）、`unfinished_after`（预计还剩什么）；
- 每阶段结束写 `handoff.md`（报告 + 失败项 + 复核意见 + 工作区状态）并追加 `room/projects/<项目>/PROGRESS.md`；
- 用户说「继续」时，大脑读这些材料产出下一阶段，优先修失败项和复核问题。

## 目录

```text
agent_team/      引擎与所有运行时模块（包）
  worker.py      消息认领、群聊大脑、流水线、阶段推进
  team.py        任务卡协议：建卡 / 派发 / 验收 / 复核 / 报告
  room.py        消息总账
  room_web.py    网页工作台（群聊界面）
  codex_bridge.py 调 Codex CLI 的唯一出口（按注册表的 cli 分流到 claude / api）
  api_providers.py API 提供方：地址、key（环境变量名或本机保存的那份）
  api_bridge.py  调 OpenAI 兼容接口（chat/completions）的唯一出口
  start_all.py   启停与状态
  cli.py         统一命令入口 agent-team
bin/             兼容壳（老命令 python bin/xxx.py 仍可用）+ 系统启动脚本
config/          team.json：模型、超时、重试
agents/          复核者的系统提示词
tests/           回归测试（704 项）
docs/            设计文档
room/ tasks/     运行时账本与任务卡（不进产物仓库）
```

## 已知限制

- 大脑一轮回复 10–40 秒：没有流式输出，靠"正在思考"指示器兜住体验；
- 单项目串行：同一个引擎一次只跑一条流水线；
- API 通道只有文字：OpenAI 兼容接口改不了文件，所以 API 模型只能做协调 / 规划 / 复核，执行者必须是本机 CLI；
- 成本随上下文增长：一次执行 2–30 万 tokens，一圈完整流水线约 20 万起；
- 只在 Windows 上验证过（PowerShell 5.1 + GBK 控制台），macOS/Linux 路径已留但未长期跑。
