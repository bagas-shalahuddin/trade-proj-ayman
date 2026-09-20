#!/usr/bin/env bash
# Linux: MT5's Python package is Windows-only, so this must run INSIDE Wine's Python.
# See README "Linux". Run from this folder after filling .env.
set -euo pipefail
set -a; source .env; set +a
python server.py --selftest
exec python server.py "$@"
