"""向后兼容壳：真正的实现在 agent_team/reset_ledger.py（python -m agent_team.reset_ledger）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_team.reset_ledger import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
