# 安装与首次启动

## 必需环境

| 环境 | 用途 | 检查命令 |
| --- | --- | --- |
| Python 3.10 或更高 | 运行协作台 | `python --version` |
| Git | 工作区识别、diff、验收和复核 | `git --version` |
| Codex CLI | 规划、执行和复核 Agent | `codex --version` |
| Codex 登录态 | 让 Codex 能实际工作 | `codex login` |

Node.js 只在你要处理 JavaScript/TypeScript 项目时需要。Windows 桌面快捷方式需要 PowerShell；独立窗口模式还需要 Edge 或 Chrome。

## Windows

在 PowerShell 中执行：

```powershell
git clone https://github.com/Lucusmaster/Cowork_with_agent.git
cd Cowork_with_agent
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
codex login
agent-team doctor
agent-team start
```

如果 PowerShell 禁止激活虚拟环境，可以直接使用虚拟环境里的 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m agent_team.cli doctor
.\.venv\Scripts\python.exe -m agent_team.cli start
```

## macOS / Linux

```bash
git clone https://github.com/Lucusmaster/Cowork_with_agent.git
cd Cowork_with_agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
codex login
agent-team doctor
agent-team start
```

## 开发和图标生成

贡献代码或重新生成桌面图标时安装开发依赖：

```bash
python -m pip install -e ".[dev]"
```

这会额外安装测试、Ruff 和 Pillow。普通使用不需要 Pillow。

## 体检结果怎么处理

```bash
agent-team doctor
```

- Python、Git、Codex CLI 任一项为错误：先安装对应环境。
- Codex 登录态为提醒：执行 `codex login`。
- 端口为空闲：正常，接下来执行 `agent-team start`。
- 启动后使用命令打印的 `?k=` 一次性链接打开网页。裸地址会显示解锁页。

## 第一次使用

打开网页后，第一句话绑定业务项目，例如：

```text
工作区是 E:\my-project，这个项目叫 my-project
```

之后直接用自然语言发需求。协作台自己的 `room/`、`tasks/` 和日志不应放进业务项目的提交里。

## 安全边界

默认只监听 `127.0.0.1`。当前版本没有 HTTPS，不能把服务绑定到 `0.0.0.0` 或直接暴露到局域网；详见 [SECURITY.md](../SECURITY.md)。
