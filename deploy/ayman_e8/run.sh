#!/usr/bin/env bash
# Linux: MT5's Python package is Windows-only, so this must run INSIDE Wine's Python.
# See README "Linux". Run from this folder after filling .env.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo ".env not found. cp .env.example .env, then edit it." >&2; exit 1; }
# Wine: the MetaTrader5 package is a Windows extension module, so the interpreter must
# be the Windows Python INSIDE the same prefix -- Ubuntu's python3 cannot import it.
#   export WINEPREFIX=$HOME/.wine MT5_PYTHON="wine python"
PY=${MT5_PYTHON:-python}
$PY server.py --selftest
exec $PY server.py "$@"
