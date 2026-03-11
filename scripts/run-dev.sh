#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/src"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"
RELOAD="${RELOAD:-true}"
CONFIG_PATH="${CONFIG_PATH:-$(pwd)/config.yaml}"
PYTHON_BIN="${PYTHON:-python}"

args=(start --host "$HOST" --port "$PORT" --config-path "$CONFIG_PATH")
if [ "$RELOAD" = "true" ]; then
  args+=(--reload)
fi

exec "$PYTHON_BIN" -m outline_agent "${args[@]}"
