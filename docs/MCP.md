# MCP 工具（给别的 Agent 调你的协作台）

协作台不只会自己跑任务——它还暴露 6 个 MCP 工具，任何支持 MCP 的客户端
（Claude Code、Codex、豆包、DeepSeek Harness 等）都能直接让它干活：

> 「让 agent-team 建一个计算器，跑完把结论给我。」

传输是**标准 stdio JSON-RPC**，只用标准库实现（不引 MCP SDK，也不用额外的包）。

## 六个工具

| 工具 | 干什么 | 需要令牌 |
| --- | --- | --- |
| `team_status` | 协作台在不在跑；顺带给出最近几张卡 | 否（`/api/alive`） |
| `team_launch` | 没在跑就起，**已经在跑就不动**；返回一次性打开链接 | 否 |
| `team_task_create` | 把需求发进群，拆卡执行；**返回 task_id** | 是 |
| `team_task_wait` | 阻塞到终态（passed/failed/exec-failed/stopped），给结论 | 是 |
| `team_task_list` | 最近的卡 | 是 |
| `team_task_stop` | 停掉一张卡：引擎在下一个步骤边界停下，记为 stopped（`POST /api/control`） | 是 |

`team_task_create` 为什么不是「发完就返回消息 id」：`POST /api/post` 只回消息 id，
任务卡是后台 worker 拆好后建的。所以这个工具会盯住新消息和任务列表，**等到真任务号**
才返回；等不到就说"已收下、还在规划"——不会编一个假 id 给你。

## 怎么接

### Claude Code

```bash
claude mcp add agent-team -- python -m agent_team.mcp_server
```

### Codex（`~/.codex/config.toml`）

```toml
[mcp_servers.agent_team]
command = "python"
args = ["-m", "agent_team.mcp_server"]
startup_timeout_sec = 20
```

### 通用 JSON 配置（豆包 / DeepSeek Harness / 其它客户端）

```json
{
  "mcpServers": {
    "agent-team": {
      "command": "python",
      "args": ["-m", "agent_team.mcp_server"],
      "cwd": "E:\\多Agent协作"
    }
  }
}
```

- 装过 `pip install -e .` 就不需要 `cwd`；否则用 `cwd` 指到仓库根，或写绝对路径的 `--directory`；
- 协作台的房间目录由 `AGENT_TEAM_HOME` 决定（默认仓库下的 `room/`）。客户端如果和协作台
  不在同一个环境里，记得把这个变量一起配上，否则会读到另一个空房间。

### 没有 MCP 支持的客户端 / shell 脚本

同一套逻辑有 CLI 入口：

```bash
agent-team mcp --action list                          # 看工具
agent-team mcp --action call --tool team_status --args '{}'
agent-team mcp --action call --tool team_task_create \
    --args '{"request":"给计算器加一个 sqrt","client":"my-script"}'
```

## 几个必须知道的约束

1. **令牌只走 Header**。`room/web-token` 里的长期令牌放在 `X-Team-Token` 里，
   绝不进 URL（0.9.12 起带 `?token=` 的请求一律 401）。工具里已经这么做了，别自己拼 URL。
2. **`team_launch` 给的是「一次性」链接**（`?k=`）。它只用于人打开浏览器；
   agent 不需要它，拿到了也别往日志里写。
3. **没人开页面时服务不会自己关**；但如果**曾经**开过页面又关掉，守护逻辑会在约 90 秒无心跳后
   收工（`team_status` 会带上 `last_stop.reason` 说明原因）。纯 agent 用法不受影响——
   从没人开过页面就不会触发这条。正在跑的任务也不会被打断（守护会等任务结束）。
4. **调用方身份记在消息的 `meta.client` 上**，不在任务卡里。任务卡的 `owner` 是执行者
   （`codex`），没有"谁提的需求"这个字段；要留痕就看群消息。
5. **`team_task_wait` 默认等 30 分钟**。长任务记得显式给 `timeout`，或者先
   `team_task_create` 拿到 id、稍后再 `wait`。**`wait` 没有副作用，可以重复调**：
   客户端自己的调用超时（有的 60 秒就掐）比 1800 短时，传个小一点的 `timeout` 分批等，
   被掐断了就再 `wait` 一次，不会丢任务。
6. **`team_task_stop` 先验任务存在**再写指令（`/api/control` 不校验任务号，我们不跟着撒谎）：
   任务号不存在直接报错、已经结束就如实说「已经是 passed/failed，没写指令」。
   写下去的指令是**一条共享槽位**（`room/control.json`，后到的覆盖先到的），
   所以别同时停两张卡。

## 自检

```bash
python -m unittest tests.test_mcp_server -v      # 36 项
python .tmp/probe17/mcp_e2e.py                   # 真起一个 MCP 子进程走完整握手
```