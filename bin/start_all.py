"""向后兼容壳：真正的实现在 agent_team/start_all.py（python -m agent_team.start_all）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_team.start_all import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
