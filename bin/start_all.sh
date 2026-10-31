#!/usr/bin/env bash
# ASCII-only wrapper for macOS / Linux / Git Bash.
# Usage: bash bin/start_all.sh [--stop|--status]
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v python3 >/dev/null 2>&1; then
  py=python3
else
  py=python
fi
exec "$py" "$root/bin/start_all.py" "$@"