#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"
RELOAD="${RELOAD:-true}"
CONFIG_PATH="${CONFIG_PATH:-$(pwd)/config.yaml}"
PYTHON_BIN="${PYTHON:-python}"
UV_BIN="${UV:-uv}"

args=(start --host "$HOST" --port "$PORT" --config-path "$CONFIG_PATH")
if [ "$RELOAD" = "true" ]; then
  args+=(--reload)
fi

exec "$UV_BIN" run --python "$PYTHON_BIN" outline-agent "${args[@]}"
