# Windows: run from this folder, in an ELEVATED shell (port 80). Fill .env first.
#   .un.ps1           dry run
#   .un.ps1 --live    sends real orders
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .env)) { throw ".env not found. Copy-Item .env.example .env, then edit it." }
python server.py --selftest
python server.py @args
