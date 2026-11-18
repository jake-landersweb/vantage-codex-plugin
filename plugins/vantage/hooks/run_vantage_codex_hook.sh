#!/bin/sh

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

python_path="${VANTAGE_CODEX_PYTHON:-}"
if [ -z "$python_path" ]; then
  python_path="$(command -v python3 2>/dev/null || true)"
fi

if [ -z "$python_path" ] || [ ! -x "$python_path" ]; then
  exit 0
fi

if [ "$python_path" = "/usr/bin/python3" ] && command -v xcode-select >/dev/null 2>&1; then
  if ! xcode-select -p >/dev/null 2>&1; then
    exit 0
  fi
fi

current_hook_script="${script_dir}/../current/vantage_codex_hook.py"
if [ -f "$current_hook_script" ]; then
  exec "$python_path" "$current_hook_script"
fi

hook_script=$(
  CODEX_HOME="${CODEX_HOME:-$HOME/.codex}" \
  CURRENT_PLUGIN_DIR="${script_dir}/../current" \
  VANTAGE_CODEX_PLUGIN_ROOT="${VANTAGE_CODEX_PLUGIN_ROOT:-}" \
  "$python_path" - <<'PY'
import os
import re
from pathlib import Path


def version_key(path):
    return tuple(int(part) for part in re.findall(r"\d+", path.parent.parent.name))


codex_home = Path(os.environ["CODEX_HOME"])
pointer_candidate = Path(os.environ["CURRENT_PLUGIN_DIR"]) / "vantage_codex_hook.py"
explicit_root = os.environ.get("VANTAGE_CODEX_PLUGIN_ROOT", "").strip()
candidates = []

if explicit_root:
    candidates.append(Path(explicit_root) / "hooks" / "vantage_codex_hook.py")
if pointer_candidate.is_file():
    candidates.append(pointer_candidate)

for cache_root in (
    codex_home / "plugins" / "cache" / "workspace-directory" / "vantage",
    codex_home / "plugins" / "cache" / "vantage-codex-plugin",
):
    if cache_root.is_dir():
        candidates.extend(
            path / "hooks" / "vantage_codex_hook.py"
            for path in cache_root.iterdir()
            if path.is_dir()
        )

valid_candidates = [candidate for candidate in candidates if candidate.is_file()]
if valid_candidates:
    print(max(valid_candidates, key=version_key))
PY
)

if [ -z "$hook_script" ] && [ -f "${script_dir}/vantage_codex_hook.py" ]; then
  hook_script="${script_dir}/vantage_codex_hook.py"
fi

if [ -n "$hook_script" ]; then
  exec "$python_path" "$hook_script"
fi
