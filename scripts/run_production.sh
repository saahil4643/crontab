#!/usr/bin/env bash
# Production entrypoint for cron 62 on Linux servers.
# GuruFocus needs a display; xvfb-run provides a virtual framebuffer when none exists.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if [[ -x "${ROOT}/venv/bin/python" ]]; then
  PYTHON="${ROOT}/venv/bin/python"
fi

export GURUFOCUS_USE_HEADED="${GURUFOCUS_USE_HEADED:-1}"

if command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a --server-args="-screen 0 1920x1080x24" \
    "$PYTHON" "${ROOT}/run.py" "$@"
else
  echo "warning: xvfb-run not found; GuruFocus may fail without a display" >&2
  exec "$PYTHON" "${ROOT}/run.py" "$@"
fi
