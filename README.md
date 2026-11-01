# 协作台 · agent-team

[English](README.en.md) · [架构](docs/ARCHITECTURE.md) · [路线图](docs/ROADMAP.md)

[![CI](https://github.com/Lucusmaster/Cowork_with_agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Lucusmaster/Cowork_with_agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)


一个跑在本机的多 Agent 协作系统：**DeepSeek 当大脑**（读群聊、拆任务、判验收），**Codex 当执行者**（改代码、跑测试），**只读的 Codex 当复核**。决策权和执行权分开，验收只看证据。

你不用在两个 agent 之间复制粘贴——它们在一个群里，互相看得到对方说的话。

```text
你 ──▶ 群聊界面 ──▶ 大脑（读上下文、回话、拆卡）──▶ Codex 执行 ──▶ 只读复核 ──▶ 报告回到群里
```

- 网页群聊：`agent-team start` 会打印带令牌的地址（默认 `http://127.0.0.1:8899`）
- 说人话就行，需求模糊也能接
- 关掉网页即收工（有任务在跑时会等它跑完）

## 现在长这样：聊天式工作台

界面重做成了对话产品：左边是工作区和历史会话，中间是消息流，右边是 Agent 状态、当前任务、测试结果和代码改动。**你只输入自然语言**，不碰任务号、不碰命令、不用把结果复制给下一个 Agent。

```text
┌──────────────┬────────────────────────────┬──────────────────┐
│ 工作区        │ 顶部：工作区路径（可验证）   │ Agent 状态        │
│ 历史会话      │ ──────────────────────────  │ 当前任务卡        │
│              │ 你：给登录页加记住我          │ 测试结果          │
│              │ 协调：我理解成…这就是计划      │ 代码改动          │
│              │ 规划：拆成 3 步，验收是…      │                  │
│              │ 执行：改了 src/login.py     │                  │
│              │ 验收：5 个测试全过           │                  │
│              │ 复核：PASS（另一个模型看的）  │                  │
│              │ ──────────────────────────  │                  │
│              │ 输入框（Enter 发送）          │                  │
└──────────────┴────────────────────────────┴──────────────────┘
```

五个角色各发各的消息，都落在同一个会话里：

| 角色 | 干什么 | 不干什么 |
| --- | --- | --- |
| 协调（coordinator） | 理解需求、决定要不要动手、最后汇总 | 不直接改代码 |
| 规划（planner） | 拆成可验收的任务卡，写清验收命令 | 不碰代码 |
| 执行（executor） | 在工作区里改文件、跑测试 | 不改验收标准 |
| 验收（tester） | 在沙箱外跑验收命令，只认退出码 | 不听执行者自述 |
| 复核（reviewer） | 换个模型只读审 diff，专挑假通过 | 不能改文件 |

消息协议（每个会话一个 JSONL，`room/sessions/<id>.jsonl`）：

```json
{
  "id": "msg-021",
  "session_id": "chat-001",
  "role": "assistant",
  "agent": "executor",
  "task_id": "T-261031-155003",
  "content": "改动完成：textstats.py 加了 count_vowels，补了对应测试",
  "artifacts": ["tasks/T-261031-155003/exec-01.last.md", "workspace:textstats.py"],
  "status": "complete",
  "created_at": "2026-10-31T15:50:31"
}
```

Agent 之间**只传文件引用**，不把几万字日志互相灌：正文留在 `tasks/<ID>/`，消息里只放 `artifacts`。点开网页上的 📄 就能看原文。

失败就是失败：验收退出码非 0、改动越界、复核判 `NEEDS_FIX`，消息标红并写明原因，任务终态记为 `outcome=failure`，不会因为流程走完就显示成功。

## 一句话拆成多件事，并行做预审

用户说"这两件事分开做"，它不会硬塞进一个任务卡，而是拆成两条，按依赖顺序一条条过验收：

```text
你     给 textstats 加 count_spaces，再加 count_punct，两件事分开做
协调   我拆成 2 步：先实现并验收 count_spaces，通过后再实现 count_punct
预审   37.3s（串行要 69.2s）        ← 风险预审和验收口径是两个只读分析，同时跑
规划   （第 1/2 步）4 条验收标准，2 个允许改动的路径
执行   …
验收   第 1 轮验收通过
复核   复核通过
规划   （第 2/2 步）…
协调   2 步全部做完了
```

某一步没过，后面的步骤不会硬跑，协调者会停下来问你要不要先修这一步。

**并行分析**：一个需求进来，除了大脑给计划，还有两个只读分析同时跑——复核者预判"这个仓库里最容易踩的坑"，
验收者给"就能这么验收"的口径。墙钟时间取两者最大值，不是相加；结果落进任务卡的 `prescan.md`，
后面的复核能看到当初的预判。不想要这份开销就设 `policy.parallel_analysis: false`。

## 界面底座

网页的样式收敛在一套变量上，改皮肤只改变量，不动结构：

| 组 | 取值 |
| --- | --- |
| 字号 | 11 / 12 / 13 / 15 / 19（标签 / 次级 / 正文 / 小标题 / 大标题） |
| 间距 | 4 / 8 / 12 / 16 / 24 / 32（4px 栅格） |
| 圆角 | 6 / 10 / 14 / pill |
| 动效 | 140ms + `cubic-bezier(.2,.7,.3,1)`，`prefers-reduced-motion` 下全部关掉 |

所有文字色对四种表面色都 ≥ 4.5:1（WCAG AA）；按钮有 hover / active / focus-visible / disabled 四态，
焦点环统一 `--ring`；选中文字、输入光标、占位符、滚动条都主题化过。
断点两个：1180px（右栏收窄、次要统计隐藏）和 620px（收成一栏，侧栏与检查器让位）。

## 会话管理

左栏每个会话行 hover（或键盘聚焦）会出现「⋯」：

- **归档**：立刻从主列表收进底部可折叠的「已归档 · N」分组，消息一条不删；归档会话不能直接打开，
  行尾有「恢复」按钮，恢复后回到主列表。归档当前会话时，活动会话自动切到最近一个未归档的。
- **删除**：要确认框（显示标题和消息条数），确认后会话从索引里移除、消息文件挪进
  `room/sessions/trash/<会话>-<时间>.jsonl`——界面上撤销不了，但文件还在，需要时能手动捡回来。

接口：`POST /api/session/archive {id, archived}`、`POST /api/session/delete {id, confirm:true}`
（没有 `confirm:true` 直接 400，防脚本误删）。一个会话都不剩时，发第一条消息才会新建。

## 选工作区

不用盲输路径：点「浏览…」打开一个目录索引，可以一级级点进去看，选之前就能看到这个目录的体检结果
（是不是 git 仓库、当前分支、这一层多少文件、能不能写、是不是已经建过任务账本），再点「确认使用这个工作区」。

- 面包屑每一段可点，「↑ 上一级」/ Backspace 往回走，顶部可切盘符；
- 点目录行 = 进入；行尾「选它」= 只选中、不进入；
- 键盘：`Esc` 关闭、`↑/↓` 移动、`Enter` 进入；
- 体检不过（不是 git 仓库、不可写）时确认按钮是灰的，并写出原因；
- 没绑工作区时，左侧、欢迎区和右侧检查器都有「选择工作区」入口；打开时默认停在上次用过的工作区/路径，没有就落在主目录。

接口：`GET /api/browse?path=<绝对路径>`，只列目录（不吐文件名），`entries` 最多 200 条，噪音目录（`.git`、`node_modules`、`__pycache__` 等）列在最后并压暗。

## 引擎活着、心跳、按钮

长任务执行期间（Codex 一次能跑一两分钟），引擎有独立心跳线程，每 10 秒刷新一次，
**不会被主循环占住**。界面上的状态分成三档，不混为一谈：

| 状态 | 界面 | 按钮 |
| --- | --- | --- |
| `running` / `busy` | 绿「引擎在跑」 | 全部可用 |
| `stalled`（进程活着、心跳旧） | 黄「引擎在跑，心跳 Ns 没刷新（可能在跑长任务）」 | 照样可用 |
| `dead` / `none` | 灰「引擎没在跑」 | 置灰 |

判断"引擎在不在"只看**进程**，不看心跳——心跳旧只能说明它可能在忙。
`agent-team status` / `agent-team stop` 也按同一口径：心跳旧但进程还在，`stop` 会正常把它杀掉。

规划阶段（还没建出任务卡）右栏显示占位卡「正在规划：<你的需求>」，暂停/停止已经可用。

## 长会话

消息默认只渲染最近 120 条，顶部「加载更早的 120 条」往前翻；轮询只追加新消息，不重画整页。
接口：`GET /api/messages?before=<id>&limit=120`（`limit` 上限 500）。

**断层自愈**：如果两次轮询之间消息多到一页装不下（客户端发现「回来的第一条 id 比本地最后一条大 1 以上」），
页面会自己回补那段缺口，最多 5 页；补不齐就在流顶部明说「历史消息有断层（缺 N 条）」并给一个「重试补齐」按钮
（放宽到 20 页），不会静默丢消息。

## 过程中你能插手

跑起来之后不是只能干等。右栏当前任务下面有四个按钮，或者直接用接口：

| 按钮 | 效果 | 什么时候生效 |
| --- | --- | --- |
| 暂停 | 引擎在**下一个步骤边界**停下，不再动文件 | 1~2 秒内，界面 pill 变「已暂停」 |
| 继续 | 从停下的那一步接着跑 | 立刻 |
| 停止 | 这一轮作废，任务记为 `stopped`，不会显示成完成 | 下一个边界 |
| 重新执行 | 重跑当前这一步（比如你觉得执行者理解歪了） | 下一个边界 |

```bash
curl -X POST http://127.0.0.1:8899/api/control \
  -H "X-Team-Token: $(cat room/web-token)" \
  -d '{"action":"pause","task_id":"T-261031-161853"}'
```

想让它在动手前先问你一句，把 `config/team.json` 的 `policy.require_approval` 改成 `true`：
计划出来后会停在「等你批准」，你点批准才派给执行者。

**访问令牌**：网页只绑 `127.0.0.1`，并且每个请求都要带凭证（`room/web-token`，启动时自动生成）。

启动器打印的链接自带 `?token=`，那条链接**只能用一次**：服务端校验通过就 `302` 到一个干净的地址，
同时把凭证写进 `HttpOnly; SameSite=Strict` 的 Cookie（`team_token`，30 天），地址栏里不会再留下令牌。
不带令牌打开会要求你粘贴一次，走 `POST /api/unlock` 换 Cookie。

- `/api/*` **不接受** URL 上的 `?token=`（历史书签、Referer 里的旧令牌都失效），只认 Cookie 或 `X-Team-Token` 请求头；
- 所有写操作（`/api/*` 的 POST）都校验 `Sec-Fetch-Site` / `Origin`（必须同源回环）并要求 `Content-Type: application/json`，挡跨站请求；
- 解锁接口按来源 IP 限制：1 分钟错 5 次后返回 429。

给脚本/CI 用还是老办法：`-H "X-Team-Token: $(cat room/web-token)"`（浏览器跨站发不了自定义头，所以这条留着是安全的）。

> 安全边界：只支持 `http://127.0.0.1`。Cookie 故意没加 `Secure`（http 下加了浏览器会直接丢弃），
> 也没有 HTTPS。**在补上 HTTPS / 反向代理之前，不要把它绑到 `0.0.0.0` 或对外网开放。**

### 装成包用（不是从仓库里直接跑）

```bash
pip install agent-team-workbench
export AGENT_TEAM_HOME=~/my-workbench   # 账本（tasks/ 和 room/）放这里
agent-team start
```

`AGENT_TEAM_HOME` 不设时默认 `~/.agent-team`。**账本绝不会写进 `site-packages`**，
`config/`、`agents/` 走包内自带副本，你自己的工作区路径另说。
## 快速开始

依赖：Python ≥ 3.10、git、Codex CLI 且已 `codex login`（跑 JS/TS 项目再装 node）。

```powershell
python -m agent_team.cli doctor        # 1) 体检：Python / git / Codex 登录态 / 端口
python -m agent_team.cli start         # 2) 拉起协作台 + 引擎
# 3) 打开 http://127.0.0.1:8899，第一句说：
#    "工作区是 E:\我的项目，这个项目叫 XXX"
```

装成命令也行（可选，之后就能用 `agent-team`）：

```powershell
pip install -e .
agent-team start
```

老写法仍然可用（`python bin\start_all.py`、`python bin\room.py ...`），`bin/` 里现在只是兼容壳。

## 项目结构

```text
agent_team/           运行时包（引擎、协议、网页、桥）
  worker.py           协调器：认领消息、驱动五个 Agent、推进阶段
  team.py             任务卡内核：建卡/派发/验收/复核/报告
  session.py          会话与消息模型（room/sessions/*.jsonl）
  agents.py           统一 Agent 适配层（run(role, context, workspace, task)）
  workspace.py        工作区校验与路径边界
  room.py             消息总线（旧接口兼容层）
  room_web.py         聊天式工作台（三栏、工作区选择器、artifact 预览）
  codex_bridge.py     调 Codex CLI 的唯一出口
  cli.py              统一命令入口
  launcher.py         双击启动器（已在跑就只开页面）
bin/                  兼容壳 + 系统启动脚本（.bat/.ps1/.sh）
config/team.json      模型、超时、重试
agents/               大脑与执行者的契约提示词
tests/                回归测试（479 项）
docs/                 架构与路线图
room/ tasks/          运行时账本与任务卡（不进产物仓库）
```

## 桌面启动（Windows）

双击桌面的 **agent-team** 图标就能用，不用记命令：

```powershell
agent-team install-desktop      # 装一次：桌面生成 agent-team.lnk
```

双击后会：没在跑就起服务 → 等到页面真能打开 → 用**一次性链接**开浏览器；
已经在跑就只打开页面、不重启（重启会打断正在执行的任务）。想从命令行等价调用：

```powershell
python -m agent_team.launcher                # 缺省 8899
python -m agent_team.launcher --port 8900
```

## 文档

- [架构](docs/ARCHITECTURE.md)：角色、消息总线、状态机、验收协议、守卫
- [路线图](docs/ROADMAP.md)：已完成与下一步
- [更新日志](CHANGELOG.md)
- [参与开发](CONTRIBUTING.md)：红线与开发循环
- [改造路线](docs/REFACTOR-PLAN.md)：从任务流水线到对话式工作台

## 常用命令

```powershell
agent-team start [--restart]   # 启动（--restart 改了代码后用）
agent-team stop                # 停掉
agent-team status              # 服务与进行中任务
agent-team doctor              # 体检
agent-team task --text "..."   # 命令行交任务（自动启停）
agent-team say --text "..."    # 以大脑身份往群里说话
agent-team tail -n 20          # 看群里最近消息
agent-team wait -t T-xxxx      # 等某张卡到终态
```

## 发给别人

```powershell
python bin\reset_ledger.py --yes     # 清掉 room/ 历史与 tasks/ 任务卡（不动 bin/ tools/ config/ docs/）
```

然后把整份目录打包发走。对方解压到任意位置，`python bin\doctor.py` 按提示补齐即可。
`room/worker.lock`、运行日志、`.codex-home/`、`.tmp/` 都不进版本库；锁文件带仓库标识，别人那份不会跟本机的实例打架。

## 可视化协作台（先看这个）

```powershell
python bin\start_all.py              # 或 powershell -ExecutionPolicy Bypass -File bin\start_all.ps1
# 浏览器打开 http://127.0.0.1:8899
```

一个命令拉起两个后台常驻进程（脱离终端，关掉窗口也继续跑）。
**关掉这个网页就等于收工**：页面会尽力发一次告别，服务端等几秒（留给刷新）后
停掉引擎并退出，状态写进 `room/session_state.json`；用本 skill 的会话会收到「协作已结束」。
想让它脱离网页常驻（例如挂着跑长任务），用 `--no-autostop` 启动，或设环境变量 `AGENT_TEAM_AUTOSTOP=0`。

两个后台进程：

| 进程 | 作用 |
| --- | --- |
| `bin/room_web.py` | 协作台网页（三栏工作台） |
| `bin/worker.py` | 工作流引擎：**3 秒轮询群聊，有新任务自动跑完整流水线** |

三栏工作台：

| 区域 | 内容 |
| --- | --- |
| 左 | 成员实时状态（大脑/执行/复核，谁在干活）+ 任务列表（状态、轮次） |
| 中 | 协作流：你说的话、分工、执行事件、验收结果；底部随时输入下一条任务（执行中也能发，排队） |
| 右 | 当前任务的流水线（计划 → 执行 → 验收 → 复核）、验收标准、最近一轮的退出码/耗时/tokens、验收输出、复核结论、改动统计 |

数据全部来自 `room/messages.jsonl` 和 `tasks/<ID>/`，不额外存数据库。
流水线事件由 `bin/team.py` 在 dispatch/verify/review 时自动写入（`kind=event`），所以面板是实时的，不是摆设。

## 这是一个群聊，不是任务表单

中间那个输入框就是聊天框，跟平常聊天一样用。几件事都能用大白话说：

| 你说 | 群里发生什么 |
| --- | --- |
| 「工作区是 E:\my-project，这个项目叫 my-project」 | 记进 `room/project.json`，页面底栏显示当前项目 |
| 「把登录页做出来」「帮我把导出加上」 | 大脑先回一句理解，再拆卡交给 Codex；验收和复核完，Codex 的汇报、复核者的结论都会贴进群里 |
| 「继续」 | 接着上一阶段做（自动带报告/失败项/复核意见） |
| 「测试挂了帮我修」 | 同上，失败用例原文会带进任务 |
| 需求太模糊 | 大脑按最合理的理解动手，并在群里说明假设；连项目都没绑就先问一句 |

群里每个角色说的话都进同一条消息流（`room/messages.jsonl`），所以谁都能看到谁说了什么：

```powershell
python bin\room.py post --as deepseek --text "我先按最小可用做，别改公共配置"
python bin\room.py tail -n 20
```

## 分阶段做项目：说「继续」就接着上一阶段

大项目不是一次跑完的。每阶段结束后引擎会留下交接材料，用户说「继续」（或点看板底部
「继续上一阶段」）就自动接上：

| 交接内容 | 位置 |
| --- | --- |
| 上阶段做了什么 / 没做什么 | `tasks/<ID>/report.md` |
| 失败用例原文 | `tasks/<ID>/verify-*.txt` |
| 复核意见 | `tasks/<ID>/review-*.last.md` |
| 阶段交接材料（含上述全部 + 工作区状态） | `tasks/<ID>/handoff.md` |
| 跨阶段进度台账 | `room/projects/<项目名>/PROGRESS.md` |

新卡会记 `stage`、`continues`（上一阶段任务号）、`unfinished_after`（预计还剩什么），
规划器按"优先修失败项、再逐个加新功能"的顺序产出下一阶段，一轮只做能扎实做完的量。

## 工作流引擎：发完就不管了

你在页面上发一句需求，`worker.py` 在 3 秒内认领并自动走完：

```text
规划（Codex 当参谋出任务卡：标题/目标/验收标准/验收命令/允许改动范围）
  → 建卡（tasks/<ID>/task.json + plan.md）
  → 派发（Codex 执行，改为改文件跑测试）
  → 验收（本地跑验收命令，退出码说话）
  → 复核（Codex 只读审 diff，专挑假通过和越界）
  → 报告 → 回帖到群里
```

失败会自愈：

| 情况 | 引擎怎么做 |
| --- | --- |
| 验收不过 | 写 `verdict-NN.md` 要求返工，自动再派一轮（最多 2 轮） |
| 复核 `NEEDS_FIX` | 把复核意见原文变成返工判决书，再修一轮、再验收、再复核 |
| 两轮仍不过 | 停下，在群里说清卡在哪，等你拍板 |
| 需求太短（"继续""好"） | 不猜，反问你要改哪个文件、达到什么效果 |

安全阀：验收命令走白名单（只允许 unittest/pytest/py_compile/node --test/npm test 这类），
含 `rm`、`del`、`>`、`&&` 的一律丢弃；单实例锁防重复执行；全程写 `room/worker.log`。

## 现在是谁在干

| 角色 | 当前由谁担任 | 说明 |
| --- | --- | --- |
| 大脑（Brain） | DeepSeek Harness 本会话（`deepseek-v4.1-flash-1017`） | 写任务卡、写计划、审产物、下验收结论 |
| 执行者（Executor） | Codex CLI（模型、推理档在 `config/team.json` 里配） | 按任务卡改文件、跑测试，做不到就如实报错 |
| 复核者（Reviewer） | Codex CLI（另一个模型，`read-only` 沙箱） | 只读审 `git diff`，专挑"测试假通过"和越界改动 |
| 人 | 你 | 发任务；需要拍板时回一句 |

执行者是可替换的：`agent_team/codex_bridge.py` 是唯一的模型出口，换别的 CLI Agent 只需要改这一层。

## 已经验证的成果

一次真实任务（实现一个命令行小工具）跑完三轮闭环：

| 轮次 | 谁做了什么 | 耗时 | 结果 |
| --- | --- | --- | --- |
| 1 | Codex 写下 `tools/wordstat.py` + 测试 | 85s | 测试断言自己写错 → 大脑判 `repair` |
| 2 | Codex 按验收结论修断言、删掉 `SkipTest` 兜底 | 96s | 还剩 Windows 换行导致字节数错 → 大脑判 `repair` |
| 3 | Codex 改成二进制写入固定字节 | 84s | 4 个测试全绿 → 大脑判 `pass` |

三轮的提示词、Codex 原始输出、验收日志、判定结论全部落在 `tasks/<ID>/`，可复盘。

## 工作区规则（重要）

**一句话：协作的账留在这里，干活的产物留在项目里。**

| 东西 | 放哪 |
| --- | --- |
| 群聊记录（`room/`）、任务卡、计划、验收证据、复核结论、报告（`tasks/<ID>/`） | **协作台自己的目录** —— 协作会话记录一律留在这里 |
| Codex 会话日志、临时文件 | 本仓库 `.codex-home/`、工作区 `.tmp/`（通过 `CODEX_HOME`、`TEMP`/`TMP` 重定向，不写 C 盘用户目录） |
| 被改的代码、新建的文件、测试 | **任务卡 `workspace` 指向的项目目录**（`new --workspace`，例如 `E:\my-project`） |

也就是说，让它去改一个业务项目时：

- 群里聊了什么、下了什么判断、验收怎么过的 → 全在协作台的 `tasks/`、`room/`
- 业务代码怎么改的 → 全在那个项目的目录里，协作台一个字节都不动

**这条有硬约束兜底**：`verify` 会检查协作仓库有没有出现 `tasks/` `room/` 之外的改动；
一旦执行者把代码写进协作仓库，验收直接判 **FAIL**，不会悄悄混过去。

`.codex-home/` 故意**不按项目各放一份**：Codex 会在 home 里塞插件缓存和 sandbox 二进制，
实测一份约 450MB，每个项目复制一份是纯浪费。用 `AGENT_CODEX_HOME` 可以改位置。

## 目录结构

```
agent_team/codex_bridge.py  执行者桥：定位 codex.exe、超时、产物落盘
agent_team/team.py          协议 CLI：建卡 / 派发 / 验收 / 总览 / 报告
agent_team/worker.py        引擎：认领消息、群聊大脑、流水线、阶段推进
tasks/<任务ID>/             task.json、plan.md、prompt-NN.md、exec-NN.*、verify-NN.txt、
                            verdict-NN.md、review-NN.last.md、handoff.md、report.md
```

## 一轮协作怎么走

```powershell
# 1. 大脑建任务卡（验收标准要能判真假，验收命令要能跑）
python bin/team.py new --title "..." --goal "..." `
  --acceptance "可验收判据" --verify "python -m unittest discover -s tests -t . -v" `
  --scope tools/ --scope tests/

# 2. 大脑把计划写进 tasks/<ID>/plan.md，然后派给 Codex
python bin/team.py dispatch -t <ID>

# 3. 大脑在沙箱外跑验收（Codex 自述不算数）
python bin/team.py verify -t <ID>

# 4. 复核者只读审查本轮 diff（换一个模型，换视角）
python bin/team.py review -t <ID>

# 5. 不通过就写 tasks/<ID>/verdict-NN.md（结论只能三选一），重复 2–3
python bin/team.py report -t <ID>
```

- 状态机：`planned → executed → failed/passed`，超轮次自动 `stopped`（默认最多 3 轮）。
- `verdict-NN.md` 的结论只有三种：`pass` / `repair`（给一条可执行的修复指令）/ `stop`（必须找人）。
- `task.json` 里 `scope` 是执行者唯一允许改的路径；`acceptance` 是验收判据；`verify` 是验收命令。

## 三个角色

| 角色 | 谁 | 干什么 | 约束 |
| --- | --- | --- | --- |
| 大脑 | 我（DeepSeek，DSH 会话） | 拆任务、写计划、跑验收、判通过或返工 | 不写业务代码 |
| 执行者 | Codex（默认 `gpt-6.1-terra` / `low`，可改） | 只在一个任务卡范围内改代码 | `workspace-write` 沙箱，只准动 `scope` |
| 复核者 | Codex（默认 `gpt-6-luna` / `low`，可改） | 只读审查 `git diff`，专挑"测试假通过"和越界改动 | `read-only` 沙箱，不许改文件 |

复核契约写在 `agents/reviewer.md`，输出只有 `PASS` 或 `NEEDS_FIX` 两种结论，必须给出具体位置。

> 复核者的已知短板：它的只读沙箱跑不了 `tempfile`，所以测试永远由大脑在沙箱外跑，复核者只能靠读代码判断。这也是为什么"复核 PASS"不能替代"验收 PASS"。

## 设计上的几条硬规矩

1. **决策与执行分离**：大脑不替执行者写业务代码，执行者不改验收标准。
2. **只看证据**：判定依据是验收命令退出码、真实输出、`git diff`，不采信执行者的"我改好了"。
3. **一轮一个修复目标**：同时改多处无法归因。
4. **执行者沙箱内自检受限就如实说**：Codex 的沙箱里 `tempfile` 会 `WinError 5`，所以验收一律由大脑在沙箱外跑；执行者不许用 `SkipTest`、放宽断言来"过"。
5. **桥接把 `TEMP/TMP` 指到工作区 `.tmp`**，尽量让执行者的测试能跑（这台机器上 Codex 仍受沙箱限制，故以大脑验收为准）。

## 执行者模型与实测开销

默认配置在 `config/team.json`：

| 项 | 值 |
| --- | --- |
| model | 默认 `gpt-6.1-terra`（示例值，换成你账号下可用的模型即可） |
| reasoning_effort | `low`（小任务默认；大任务用 `new --effort medium` 写进任务卡，或 `dispatch --effort medium`） |
| timeout | 900s |
| sandbox | `workspace-write` |

换模型或档位有四层，按优先级：`dispatch` 命令行参数 → 任务卡里的 `executor: {model, effort}`（`new --model/--effort` 写入）→ **运行时切换**（见下）→ `config/team.json`；环境变量 `EXECUTOR_MODEL` / `EXECUTOR_EFFORT` 会覆盖配置文件里的默认值。
每轮消耗的 tokens 会自动从 Codex 会话日志读回、写进 `task.json` 的 `rounds[].tokens`。

### 运行时换模型（不用改配置、不用重启）

三个入口，改的都是同一个运行时覆盖文件 `room/model_overrides.json`，**下一张卡生效**：

- 网页右栏：每个角色名下面有个模型小徽章，点它弹菜单选型号；
- 群里发：`/model 执行 luna`、`切换模型 复核=terra`、`/model list`、`/model 执行 reset`；
- 命令行：`agent-team model`（看）、`agent-team model set 执行 luna`、`agent-team model reset 执行`。

三条规矩（都是为了让"切错了"当场暴露，而不是跑完一轮才发现）：

1. **只认注册表里的型号**。`config/team.json` 的 `model_registry` 定义可选型号和别名（`claude`/`ds`/`luna`…），拼错直接拒绝并列出可用项；
2. **没装对应 CLI 的不许切**。注册表里写了 `claude-opus-5-5` 但本机没有 `claude` 命令 → 拒绝并说明，而不是切过去让下一张卡必然失败；
3. **协调者不可切**。它用的是引擎自己的大脑模型，换要改配置并重启引擎，页面上标虚线、接口直接拒绝。

群里只认 `/model` 或「切换模型」这种**显式前缀**：一句「用 python 换实现方式」不会被当成切模型——把用户的正经需求误判成命令，比不支持这功能糟得多。

三次任务实测（任务量级不同，只能当参考，不是严格 A/B）：

| 任务 | 执行者 | 轮次 | 总耗时 | 总 tokens |
| --- | --- | --- | --- | --- |
| 示例 A：从零实现一个小工具 | 复核模型（medium） | 3 | 265s | ≈107 万 |
| 示例 B：给工具加两个参数 | 执行模型（medium） | 3 | 547s | ≈100 万 |
| 示例 C：小改动 | 执行模型（low） | 1 | **213s** | **45.7 万** |

观察：小改动用 terra/low 一轮就通过（213s / 45.7 万 tokens），返工成本低于 terra/medium 的多轮；terra/medium 在这台机器上明显更慢（226s / 113s / 208s），token 量与 luna 接近。真正的"性价比"取决于订阅额度权重——本地看不到，只能对比桌面端 Settings → Usage 的剩余变化。

## 已知限制

- 单机、单工作区、串行执行；没有并发调度。
- 单轮耗时取决于模型和推理档：不同模型的耗时差别很大，实测 85–230s 不等；超时默认 900s，长任务自己调 `--timeout`。
- 执行者换模型：网页右栏点徽章、群里 `/model 执行 <型号>`、`agent-team model set 执行 <型号>`、
  `config/team.json`、`dispatch --model <id>`、或 `EXECUTOR_MODEL` 环境变量。
- 这套协作**不依赖**特定 Agent 框架：核心是"共享工作区 + 无头 CLI + 验收闸门"，任何能跑命令行的 Agent 都能接进来。