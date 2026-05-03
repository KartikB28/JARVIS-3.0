#!/usr/bin/env bash
# CHAPPIE start script (macOS / Linux)
# Activates the virtual environment and launches the FastAPI server.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
  echo "No .venv found. Run ./scripts/setup.sh first."
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

cd backend
echo "==> CHAPPIE running at http://localhost:8000"
echo "    Press Ctrl+C to stop."
exec python -m uvicorn main:app --host 0.0.0.0 --port 8000
