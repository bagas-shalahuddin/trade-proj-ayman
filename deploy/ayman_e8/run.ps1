# Windows: run from this folder after filling .env
$ErrorActionPreference = "Stop"
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim())
}
python server.py --selftest
if ($args -contains "--live") { python server.py --live } else { python server.py }
