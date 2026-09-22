# Fresh Windows VPS — start to first live order

Order matters: every step verifies the one before it. Budget ~1 hour, plus one
full day of dry run before `--live`.

Spec: 2 vCPU, 4 GB RAM, 40 GB SSD, Windows Server 2022. 1 vCPU / 2 GB works but
stalls during the MT5 first-login history download.

## 1. Lock the machine down first

RDP in as Administrator, then in an elevated PowerShell:

```powershell
# Windows must never reboot on its own: a restart at 09:25 NY costs you the day.
sconfig   # option 5 -> Manual     (Server 2022)
# or:
Set-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" `
  -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord -Force

# Keep the session alive and the machine awake
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Set the VPS clock to UTC. Every timestamp in this project is UTC; a local-time
box makes the session filter and the daily rollover disagree with the backtest.

```powershell
Set-TimeZone -Id "UTC"
w32tm /resync
```

## 2. Firewall: port 80, TradingView only

TradingView accepts **only ports 80 and 443**, and sends from four published IPs.
Open exactly those.

```powershell
New-NetFirewallRule -DisplayName "TradingView webhook" -Direction Inbound `
  -Protocol TCP -LocalPort 80 -Action Allow `
  -RemoteAddress 52.89.214.238,34.212.75.30,54.218.53.128,52.32.178.7
```

If your provider has its own firewall (Vultr, Contabo, AWS security groups), add
the same four IPs there too — the Windows rule alone is not enough.

## 3. Python and Git

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Close and reopen PowerShell, then confirm:

```powershell
python --version    # 3.12.x
git --version
```

## 4. MetaTrader 5

Download the terminal **from your prop firm's dashboard**, not from metaquotes.net
— the firm's build is preconfigured for their servers. Install, then:

1. Log in with the account in your `.env` (File -> Login to Trade Account).
2. Wait for the history download to finish (status bar, bottom right).
3. Tools -> Options -> Expert Advisors -> tick **Allow algorithmic trading**.
4. Open Market Watch (Ctrl+M), find gold, and note the **exact** symbol name.
   FundedNext and E8 both use suffixes on some server groups: `XAUUSD.raw`,
   `XAUUSD.m`, `GOLD`. Whatever it says goes into `SYMBOL` verbatim. A wrong name
   means every order is rejected, silently as far as TradingView is concerned.

MT5 must stay running and logged in. The Python package attaches to this terminal;
it does not start one.

## 5. The code

```powershell
cd C:\
git clone https://github.com/bagas-shalahuddin/trade-proj-ayman.git trading
cd C:\trading\deploy\ayman_e8
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Fill in: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, a fresh `WEBHOOK_SECRET`, the
`SYMBOL` you read in step 4, and the `PLAN` + `RISK_FRAC` pair for the account you
actually bought. The pairs are in the file's own comments; the wrong `PLAN` makes
the engine enforce another firm's limits.

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"   # WEBHOOK_SECRET
python server.py --selftest                                     # must print: selftest ok
```

## 6. TradingView

Pro or above; webhook alerts also require 2FA on your TradingView account.

1. Paste `ayman_e8.pine` into a new indicator on an **XAUUSD 30-minute** chart.
2. Inputs: leave the defaults. `Use HTF EMA Confirmation` stays **OFF** — on
   TradingView it is live, in the backtest it was inert, so ON is untested.
   Paste the same `WEBHOOK_SECRET` into `Webhook secret`.
3. Create alert: condition = this indicator, **"Any alert() function call"**,
   expiration open-ended, Notifications -> Webhook URL = `http://<VPS-IP>/tv`.
   Leave the message box alone; the script builds the JSON.

## 7. Dry run for one full day

Elevated PowerShell (port 80 needs it):

```powershell
cd C:\trading\deploy\ayman_e8
.\run.ps1          # DRY RUN: decides and logs, sends nothing
```

From your own machine, prove the path end to end:

```powershell
curl http://<VPS-IP>/state      # engine state as JSON
```

Then wait for a real alert. Check `trades.jsonl` — one line per alert. What you
are looking for: `"status":"dry"` with a sane `lots`, and `sl`/`tp` on the right
sides of `price`. If you see `"why":"bad secret"` in the log, the Pine input and
`.env` disagree. Nothing in `trades.jsonl` after a signal fired on the chart means
the firewall or the URL is wrong, not the code.

## 8. Live

```powershell
.\run.ps1 --live
```

Watch the first order land in MT5 with a stop attached. If it has no stop, kill
the process immediately — that is the one failure mode worth panicking about, and
`_mt5.py` is written so it cannot happen.

Keep it running across reboots:

```powershell
$a = New-ScheduledTaskAction -Execute "powershell.exe" `
     -Argument "-File C:\trading\deploy\ayman_e8\run.ps1 --live" `
     -WorkingDirectory "C:\trading\deploy\ayman_e8"
$t = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "ayman" -Action $a -Trigger $t `
  -RunLevel Highest -User "Administrator" -Password "<vps-password>"
```

MT5 also needs to come back after a reboot: put a shortcut to terminal64.exe in
`shell:startup`, and enable "save account password" in its login dialog.

## What to check on day 90

`trades.jsonl` is the point of the exercise. The backtest says +0.156R per trade
on 461 trades over 5 years. Compare the live mean against that. Passing or failing
the challenge is a coin landing; the expectancy is the measurement.
