---
name: agent-team-collab
description: Use when the user wants work done by a team of agents rather than one chat — a DeepSeek "brain" that plans and judges, a Codex CLI executor that edits code and runs tests, and a read-only Codex reviewer, through a local group-chat workbench. Covers installing or repairing the project, binding a workspace, handing tasks in by chat, watching live progress, and the stage-by-stage continuation loop. Also covers driving the workbench from another agent through its MCP tools.
---

# 协作台（agent-team）

这是个**项目**，不是一段提示词：本机跑着一个网页群聊 + 一个引擎，外面挂 Codex 当执行者和复核者。

项目位置，按这个顺序找：
1. 环境变量 `AGENT_TEAM_HOME`
2. `E:\多Agent协作`
3. 在 `~` 下找含 `agent_team/worker.py` 的目录

找到后 `cd` 进去再敲命令；`README.md` 和 `docs/` 是权威说明，冲突以代码为准。

## 五分钟上手

```bash
python -m agent_team.cli doctor    # 体检：环境、端口、模型可用性（含降级链）
python -m agent_team.cli start     # 起协作台 + 引擎
```

`start` 会打印一个**带一次性码的链接**，例如 `http://127.0.0.1:8899/?k=abcd...`。
**必须用这个链接打开**——裸地址 `http://127.0.0.1:8899` 现在只会显示解锁卡片
（0.9.12 起长期令牌禁止进 URL，`?token=` 一律 401）。链接用一次就作废，之后靠 Cookie；
令牌本体在 `room/web-token`，可以粘进页面的输入框。

第一句话就绑定项目：「工作区是 E:\某个仓库，这个项目叫 XXX」。之后正常聊：需求模糊也行，
大脑会读整段对话 + 项目设置，回话并决定要不要让 Codex 动手。

## 在会话里怎么跟它配合

```bash
python -m agent_team.cli say --as user --kind task --text "把导出功能加上"   # 替用户发一条
python -m agent_team.cli tail -n 20                                        # 看群里最近说了什么
python -m agent_team.cli wait -t T-261101-120000 --timeout 1800            # 等任务到终态
python -m agent_team.cli task --text "..."                                 # 命令行交一条，自动启停
python -m agent_team.cli say --as deepseek --text "..."                    # 以大脑身份进群说话
```

- **别轮询**：等结果用 `wait`（或 `bin/await_task.py -t ... --timeout 1800`），一次阻塞到终态。
  退出码：0 通过 / 2 未通过或中止 / 3 超时 / 4 任务不存在。`wait` 没有副作用，
  你的调用超时比任务短就先拿个小 `timeout`，被掐断了再等一次。
- **别频繁发"空"消息**：每发一条都会激活一次大脑，既慢又费钱。
- **看状态**：`python -m agent_team.cli status`、`room/typing.json`（谁在想）、
  `room/session_state.json`（是否刚收工、为什么收工）。

## 换模型（不用改配置、不用重启）

三层入口，改的都是运行时覆盖，**下一张卡生效**：

```bash
python -m agent_team.cli model                 # 看每个角色现在用哪个模型
python -m agent_team.cli model set 执行 luna    # 切执行者（别名或 id）
python -m agent_team.cli model reset 执行       # 回到配置默认
```

群里也可以 `/model 执行 luna`、`切换模型 复核=terra`；右栏每个角色名下面的徽章能点：
先选 Agent（Codex CLI / Claude Code / DeepSeek / API）再选模型。本机找不到那个 Agent 时，菜单给出
`where <命令>` 和一段可以直接问那个 Agent 的话，把回答贴回去「识别」→「保存并验证」
（存 `room/agent_clis.json`）。
规矩：只认注册表里的型号（拼错直接拒）；CLI 找不到、`--version` 跑不通、或协作台没有它的执行适配
（DeepSeek 目前只能登记）都不许切；**协调者、验收者不可切**（协调者是引擎自己的大脑，验收不调模型）。
Claude Code 当执行者时只能改文件、不能跑命令；当复核者时禁掉所有写工具。

第四组「API（OpenAI 兼容）」不用本机 CLI：先把 key 放进环境变量（如 `DEEPSEEK_API_KEY`）并
**重启协作台**，再在菜单里加提供方（只收 https 或本机 http）→「拉取模型」→ 勾选保存；
模型以 `<提供方>:<型号>` 进注册表。协作台**不保存 key**（文件里只有环境变量名）；
**API 模型不能当执行者**（改不了文件），协调 / 规划 / 复核可以。

## 从别的 Agent 指挥它（MCP）

协作台暴露 7 个 MCP 工具（stdio JSON-RPC，纯标准库）：`team_status`、`team_launch`、
`team_preflight`、`team_task_create`、`team_task_wait`、`team_task_list`、`team_task_stop`。

```bash
claude mcp add agent-team -- python -m agent_team.mcp_server   # Claude Code
python -m agent_team.cli mcp --action list                     # 看工具
python -m agent_team.cli mcp --action call --tool team_status --args '{}'
```

- **先体检再下单**：`team_task_create` 内部会先跑 `team_preflight`，
  校验不过**直接报错、不建卡、不烧轮次**。报错里带 `key` 和修复动作，照着做再重试。
  想提前知道该修什么就单独调 `team_preflight`。
- `team_task_create` 会**等到真任务号**再返回；等不到就说"已收下、还在规划"，不会编假 id。
- `team_task_stop` 停一张正在跑的卡：引擎在下一个步骤边界停，任务记 `stopped`。
  任务号不存在会直接报错、已经结束会如实说「已经是 passed」，不会假装停成功。
- 令牌只走 `X-Team-Token`（从 `room/web-token` 读），绝不进 URL。
- 调用方身份记在消息的 `meta.client` 上；任务卡里没有"谁提的需求"这个字段。
- 各客户端配置写法见项目里的 `docs/MCP.md`。

## 判"做完"只看证据

`tasks/<ID>/task.json` 的状态和 `report.md` 才算数。别信执行者的自述：
`verify-*.txt` 的退出码、`git diff`、`review-*.last.md` 里的 PASS/NEEDS_FIX 才是判据。
`finished_at` 有值（或 `report.md` 已写）才是真正收工。任务卡上如果出现：

- `out_of_scope`：某一轮改了声明范围外的文件，会报警告，但不拦截；
- `review_wrote`：复核者**动了文件**——复核本该只读，出现这个要当异常看。

## 分阶段推进

用户说「继续」= 接着上一阶段。引擎自动带上一阶段的报告、失败用例原文、复核意见、
工作区状态和 `room/projects/<项目>/PROGRESS.md`，产出下一阶段任务卡。
卡上记 `stage` / `continues` / `unfinished_after`，每阶段写 `handoff.md`。

## 关网页即收工（三种触发，容易踩）

1. 页面关掉时尽力发一次 `bye`，几秒后停掉引擎（有任务在跑会等它到终态）；
2. **没收到 bye 但心跳断了**：默认 **90 秒**没心跳就判"网页关了"——**后台标签页被浏览器
   节流时会误判**，浏览器崩溃也会。想避免就 `start` 时加 `--no-autostop`，
   或设 `AGENT_TEAM_AUTOSTOP=0`；
3. 从没人打开过页面的服务不会自己关。

注意：**别的 agent 只是调了接口并不等于"有人看着"**，别指望请求能一直续命。
真需要常驻就显式关掉自动收工；收工原因写在 `room/session_state.json` 的 `reason` 里。

## 桌面双击启动（Windows）

```bash
python -m agent_team.cli install-desktop    # 桌面生成 agent-team.lnk，之后双击即可
```

双击会：没在跑就起服务 → 等页面真能打开 → 用一次性链接开浏览器；**已经在跑就不重启**
（重启会打断正在执行的任务）。等价命令行：`python -m agent_team.launcher`。

## 红线

1. **不按名字杀进程**：只停自己启动的 pid（`start_all` 有锁和 pid 校验）。
2. **不弹黑框**：所有子进程走 `quiet_flags()`；新增调用漏了会被 `tests/test_no_console_popups.py` 抓到。
3. **产物不落账本**：业务文件只进任务卡的 workspace，不写进项目仓库。
4. **不猜工作区**：路径 → 最近的 `.git` 祖先；缺/多/不存在就停下来问用户。
5. **不越权合并发布**：合并、上线永远由人拍板。

## 故障

| 现象 | 先看 |
| --- | --- |
| 页面要求粘令牌 | 用了裸地址。用 `cli start` 打印的 `?k=` 链接 |
| 连不上 / 突然没了 | `room/session_state.json` 的 `reason`（多半是自动收工）；`cli status` |
| 发消息没反应 | `room/worker.log`、`cli status`、`room/typing.json` |
| 复核总是 NEEDS_FIX | `tasks/<ID>/baseline.diff`（任务前的脏改动）与 `review-*.last.md` |
| 「继续」不开工 | 消息里有问号/冒号会被当闲聊；确认是短指令（"继续"/"下一步"/"继续做第二阶段"） |
| MCP 下单直接报「环境校验未通过」 | 报错里的 `key`；或单独调 `team_preflight` 看全部项 |
| 端口占用 | `cli doctor`，或 `--port` 换一个 |
| 账本脏了 | `python -m agent_team.cli reset --yes`（先停服务） |

详细设计和已知限制见项目里的 `docs/ARCHITECTURE.md`、`docs/ROADMAP.md`、`docs/MCP.md`、`CONTRIBUTING.md`。