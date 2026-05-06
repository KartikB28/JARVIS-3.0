#!/usr/bin/env bash
# Run CHAPPIE as a desktop app (development mode — no packaging).

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
  echo "No .venv found. Run ./scripts/setup.sh first."
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import webview" >/dev/null 2>&1; then
  echo "Installing pywebview..."
  python -m pip install pywebview
fi

exec python desktop_app.py
