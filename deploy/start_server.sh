#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/gemini-reverse}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
CONFIG_PATH="${CONFIG_PATH:-$PROJECT_DIR/data/runtime_config.json}"

cd "$PROJECT_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Virtualenv not found: $VENV_DIR" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Runtime config not found: $CONFIG_PATH" >&2
  exit 1
fi

source "$VENV_DIR/bin/activate"
exec python scripts/start_server.py --config "$CONFIG_PATH"
