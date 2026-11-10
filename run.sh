#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UVICORN_EXECUTABLE="$PROJECT_DIR/.venv/bin/uvicorn"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"

if [[ ! -x "$UVICORN_EXECUTABLE" ]]; then
  printf 'Could not find %s. Create the project virtual environment and install requirements first.\n' "$UVICORN_EXECUTABLE" >&2
  exit 1
fi

exec "$UVICORN_EXECUTABLE" main:app --app-dir "$PROJECT_DIR" --host "$APP_HOST" --port "$APP_PORT"
