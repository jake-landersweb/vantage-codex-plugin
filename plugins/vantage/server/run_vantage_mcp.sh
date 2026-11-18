#!/bin/sh

python_path="${VANTAGE_CODEX_PYTHON:-}"
if [ -z "$python_path" ]; then
  python_path="$(command -v python3 2>/dev/null || true)"
fi

if [ -z "$python_path" ] || [ ! -x "$python_path" ]; then
  echo "Vantage telemetry setup requires Python 3. Install Python 3, then try again." >&2
  exit 4
fi

if [ "$python_path" = "/usr/bin/python3" ] && command -v xcode-select >/dev/null 2>&1; then
  if ! xcode-select -p >/dev/null 2>&1; then
    echo "Vantage telemetry setup requires Python 3. Install Python 3, then try again." >&2
    exit 4
  fi
fi

exec "$python_path" "$(dirname "$0")/vantage_mcp.py"
