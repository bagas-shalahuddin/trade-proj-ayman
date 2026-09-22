#!/usr/bin/env bash
# Linux: MT5's Python package is Windows-only, so this must run INSIDE Wine's Python.
# See README "Linux". Run from this folder after filling .env.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo ".env not found. cp .env.example .env, then edit it." >&2; exit 1; }
python server.py --selftest
exec python server.py "$@"
