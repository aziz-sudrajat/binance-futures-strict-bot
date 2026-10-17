#!/usr/bin/env bash
# Bootstrap the venv.
set -euo pipefail
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -d "$BOT_DIR/.venv" ]; then
    python3.14 -m venv "$BOT_DIR/.venv"
fi
"$BOT_DIR/.venv/bin/pip" install -q -r "$BOT_DIR/requirements.txt"
echo "Venv ready."
