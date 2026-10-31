# 拿一个空仓库试十分钟

协作台会真的改文件、跑命令，所以别拿重要仓库开第一枪。先造一个一次性的练习仓库：

```powershell
mkdir E:\scratch-demo
cd E:\scratch-demo
git init -b main
```

放一个会失败的测试进去（这样你能看到"验收不通过 → 自动返工"的完整过程）：

```powershell
@'
def add(a, b):
    return a - b          # 故意写错
'@ | Set-Content calc.py

@'
import unittest
from calc import add

class AddTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
'@ | Set-Content test_calc.py

git add -A
git commit -m "初始：一个会失败的测试"
```

然后：

```powershell
python -m agent_team.cli start
# 打开 http://127.0.0.1:8899
# 第一句：工作区是 E:\scratch-demo，这个项目叫 scratch-demo
# 第二句：把 calc.add 修好，让测试通过
```

你会看到：大脑回话 → 建卡 → Codex 改代码 → 验收命令 `python -m unittest -v` 跑过 →
只读复核给 PASS → 报告回到群里。产物在 `E:\scratch-demo`，账本在协作台的 `tasks/` 里。

想试「继续」：再发一句 `继续，给 add 加上类型标注并补一个边界测试`。

## 常见的第一天问题

| 现象 | 原因 |
| --- | --- |
| 提示"工作区没法用" | 目录不存在、不是 git 仓库、或者一句话里点了两个路径 |
| 复核一直 NEEDS_FIX | 任务开始前工作区就有未提交改动，看 `tasks/<ID>/baseline.diff` |
| 卡在建卡不动 | Codex CLI 没登录：`codex login`，再 `python -m agent_team.cli doctor` |