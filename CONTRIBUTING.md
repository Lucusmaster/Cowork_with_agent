# 参与开发

## 环境

- Windows + PowerShell 5.1（当前唯一长期验证过的组合），Python 3.10+
- Codex CLI 已登录（`codex exec` 能跑）
- 本地跑：`python -m agent_team.cli start`，打开 http://127.0.0.1:8899

## 开发循环

```powershell
python -m unittest discover -s tests -t .     # 212 项，必须全绿
python -m agent_team.cli doctor               # 体检
python -m agent_team.cli start --restart      # 改完代码重启才生效
```

## 改代码时的红线

1. **子进程必须带 `quiet_flags()`**：否则后台每次调 codex/git/python 都会闪黑框，`tests/test_no_console_popups.py` 会拦住你。
2. **不许把产物写进账本仓库**：产物只进任务卡的 `workspace`；新增路径逻辑要过 `detect_workspace`。
3. **不许高频轮询**：需要"立刻响应"就用 `room/nudge`，别把间隔改回秒级以上的忙等。
4. **验收标准要可判真假**：新增验收逻辑时，先想清楚"失败长什么样"，再写代码。
5. **协议改动要有测试**：任务卡字段、状态流转、复核判定属于协议，改一处补一处回归。
6. **不要顺手杀进程**：只停自己启动的 pid（`start_all` 有锁和 pid 校验）。

## 加一个模块

1. 放进 `agent_team/`，用包内相对导入（`from . import room`），不要 `import room`；
2. 需要独立命令就加 `main()` + `argparse`，并把子命令挂到 `agent_team/cli.py`；
3. 需要给老命令兼容就加 `bin/<name>.py` 壳（三行）；
4. 补测试，跑全量绿再提交。

## 提交信息

一句话说清"改了什么、为什么"，别写"update"。