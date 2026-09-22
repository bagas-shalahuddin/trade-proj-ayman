# Ayman M30 + volume filter -> E8 Pro $5k

Signal on TradingView (the backtested Pine, unchanged except a volume filter and an
`alert()`), execution here: alert -> risk engine with E8 Pro rules -> one MT5 order
with SL and TP1 attached.

What this is, from `research/prop_pass.py` (2026-09-20), 461 trades / 5 years:
P(pass) 79-97% at 0.75% risk, median 48 trading days, bust 2.6%. After passing the
account is worth ~$90-230 once (E8 locks the floor at breakeven on the first payout).
**Run it for the live track record.** Compare `trades.jsonl` against +0.156R/trade at
day 90; that number is what the $24 buys.

## Files

    ayman_e8.pine      Pine v6. Paste into TradingView, XAUUSD, 30-minute chart.
    server.py          webhook -> RiskEngine -> MT5. `--selftest`, dry run, `--live`
    .env.example       copy to .env, fill in. Never commit .env.
    run.ps1 / run.sh   load .env, selftest, start
    trades.jsonl       written by the server, one line per alert (gitignored)

Everything else is imported from the repo: `strategies/_risk.py` (rules),
`strategies/_mt5.py` (broker), `strategies/_plans.py` (`e8_pro_5k`). Copy the whole
repo, not this folder alone.

**Fresh Windows VPS? Follow [SETUP-VPS.md](SETUP-VPS.md) instead** — same steps,
with the firewall, clock, MT5 and scheduled-task details filled in.

## Setup

1. **TradingView** (Pro or above -- webhook alerts need it)
   - New indicator, paste `ayman_e8.pine`, add to an **XAUUSD 30-minute** chart from
     the broker feed closest to E8's (OANDA or FXCM; tick volume differs per feed).
   - Inputs: leave defaults. `Use HTF EMA Confirmation` must stay **OFF** -- on
     TradingView it is live, in the backtest it was inert; ON is an untested strategy.
   - Type a long random string into `Webhook secret`. Same string goes in `.env`.
   - Create alert: condition = this indicator, "Any alert() function call", webhook
     URL = `http://<your-vps>:8787/tv`, message left as default (the script builds
     the JSON). Expiration: open-ended.

2. **Server**
   - `pip install -r requirements.txt`
   - `cp .env.example .env`, fill MT5 login for the E8 Pro account and the secret.
   - MT5 terminal must be installed, logged in, and running on the same machine.
   - `python server.py` = DRY RUN: decides and logs, sends nothing. Watch one full
     day of alerts land in `trades.jsonl` before `--live`.
   - `python server.py --live` sends orders. `GET /state` shows the engine.
   - Override with `PORT`/`HOST` in .env if you put a reverse proxy in front.

3. **Expose port 80** to TradingView's four published IPs only:
   `52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7`. TradingView accepts
   only ports 80 and 443, so 80 it is; binding it needs an elevated shell. The secret
   is the only other guard.

## Rules enforced by the engine (E8 Pro, `e8_pro_5k`)

    daily loss     2.5% of the day's starting equity ($125)   -> flatten, no new risk
    max loss       8% of initial, static ($400)               -> flatten, halt
    profit cap     2% of initial per day ($100)               -> no NEW positions
    day-stop       1.5% intraday ($75)                         -> flatten, no new risk
    positions      1 at a time
    per trade      min(RISK_FRAC x initial, engine budget)

## Linux

`MetaTrader5` (the Python package) only exists for Windows. Options, honestly:

- **Windows VPS** ($5-10/month). Tested path. `run.ps1`.
- **Linux + Wine**: install MT5 under Wine, install Windows Python inside the same
  Wine prefix, `pip install MetaTrader5` there, run `server.py` with that Python.
  `run.sh` assumes this. Not tested from this repo.
- cTrader Open API (E8 supports cTrader) is cross-platform, but `_mt5.py` would need
  a cTrader twin. Not built.

## What differs from the backtest, stated

- Exits: the backtest evaluated SL/TP on bar close; here they are broker orders and
  fill intrabar. Stops fill slightly worse, TPs slightly better. Net unknown, small.
- Volume: relvol is a ratio at the same time of day, so the feed's scale cancels, but
  TradingView's tick volume is not Kaggle's `tick_count`. The 0.99 threshold was
  measured on Kaggle.
- Sizing: 0.01-lot steps on $5k round risk by up to +/-12%.
- Spread/slippage: the 0.64bp cost is the measured average; E8 Pro raw-spread is
  similar, but not this feed.
