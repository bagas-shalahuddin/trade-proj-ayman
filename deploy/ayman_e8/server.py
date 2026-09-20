"""TradingView alert -> RiskEngine (E8 Pro rules) -> one MT5 order with SL and TP.

    python server.py --selftest        # no MT5, no network: the parse + risk path
    python server.py                   # DRY RUN: logs what it would send
    python server.py --live            # sends real orders

The signal is NOT computed here. It is computed by `ayman_e8.pine` on TradingView --
the script that was backtested, byte for byte except the volume filter and the alert
line -- and arrives as JSON. This process owns three things only:

  1. the secret in the alert matches the one in .env (anyone who finds the URL can
     otherwise trade your account);
  2. the E8 Pro rules: daily loss, static max loss, the $100/day profit cap, and the
     intraday day-stop, all enforced by `strategies/_risk.py` BEFORE any order;
  3. one market order with the alert's stop and TP1 attached -- never without a stop.

What the backtest actually did, and what is therefore reproduced: entry at the signal
bar's close; stop at `sl`; exit at `tp` = TP1 (entry +/- 0.84R). TP1 closes the whole
position in the script, so TPFull and the break-even move never fire. Deviation stated
rather than hidden: the backtest evaluated exits on bar CLOSE; here SL/TP sit on the
broker and fill intrabar. A hard stop is non-negotiable under prop rules.

Risk per trade is min(RISK_FRAC * initial, engine.budget()): a fixed fraction, cut by
the engine when a limit is near. 0.75% is what research/prop_pass.py measured.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from strategies._mt5 import MT5Broker  # noqa: E402
from strategies._plans import get  # noqa: E402
from strategies._risk import RiskEngine  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ayman_e8")

PLAN = get("e8_pro_5k")
SYMBOL = os.environ.get("SYMBOL", "XAUUSD")
RISK_FRAC = float(os.environ.get("RISK_FRAC", "0.0075"))
SECRET = os.environ.get("WEBHOOK_SECRET", "")
POLL = 5.0                       # seconds between guard() sweeps
JOURNAL = Path(__file__).with_name("trades.jsonl")


def parse(body: bytes) -> dict:
    """Validate the alert. Raises ValueError on anything that must not become an order."""
    try:
        a = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"not json: {e}") from e
    if not SECRET or a.get("key") != SECRET:
        raise ValueError("bad secret")
    if a.get("side") not in ("buy", "sell"):
        raise ValueError(f"side {a.get('side')!r}")
    entry, sl, tp = (float(a[k]) for k in ("entry", "sl", "tp"))
    side = 1 if a["side"] == "buy" else -1
    if side * (entry - sl) <= 0:
        raise ValueError("stop is on the wrong side of entry")
    if side * (tp - entry) <= 0:
        raise ValueError("tp is on the wrong side of entry")
    return {"side": side, "entry": entry, "sl": sl, "tp": tp,
            "relvol": a.get("relvol"), "bar": a.get("bar")}


def act(b: MT5Broker, eng: RiskEngine, sig: dict) -> dict:
    """One signal -> at most one order. Returns what happened, for the journal."""
    b.sync(eng)
    if b.positions():
        return {"status": "skip", "why": "position open"}      # Ayman: one at a time
    stop_distance = abs(sig["entry"] - sig["sl"]) / sig["entry"]
    budget = eng.budget()
    want = PLAN.initial * RISK_FRAC
    if budget <= 0:
        return {"status": "skip", "why": "engine refuses", "state": eng.state()}
    order = b.place(eng, SYMBOL, sig["side"], stop_distance,
                    fraction=min(want / budget, 1.0), comment="ayman_e8")
    if order is None:
        return {"status": "skip", "why": "place refused", "state": eng.state()}
    if order.sent or b.dry_run:
        for p in b.positions():                    # attach TP1 to the position just opened
            b.modify(p, tp=sig["tp"])
    return {"status": "sent" if order.sent else "dry", "lots": order.lots,
            "price": order.price, "sl": order.stop_price, "tp": sig["tp"],
            "risk": order.risk}


def journal(rec: dict) -> None:
    with JOURNAL.open("a") as f:
        f.write(json.dumps({"ts": time.time(), **rec}) + "\n")


def serve(live: bool, host: str, port: int) -> None:
    from fastapi import FastAPI, Request, Response
    import uvicorn

    b = MT5Broker(dry_run=not live)
    b.connect()
    eng = RiskEngine(PLAN.initial, PLAN.rules)
    b.sync(eng)
    lock = threading.Lock()
    app = FastAPI()

    def guard_loop() -> None:
        """Between alerts: mark to market, fire the day-stop, flatten on a breach."""
        day = None
        while True:
            with lock:
                today = time.strftime("%Y-%m-%d", time.gmtime())
                if day != today:
                    if day is not None:
                        eng.close_day()
                    eng.start_day()
                    day = today
                b.guard(eng)
            time.sleep(POLL)

    threading.Thread(target=guard_loop, daemon=True).start()

    @app.post("/tv")
    async def tv(request: Request) -> Response:
        body = await request.body()
        try:
            sig = parse(body)
        except ValueError as e:
            log.warning("rejected alert: %s", e)
            return Response(status_code=400)
        with lock:
            rec = act(b, eng, sig)
        log.info("%s -> %s", sig, rec)
        journal({"signal": sig, **rec})
        return Response(status_code=200)

    @app.get("/state")
    def state() -> dict:
        with lock:
            b.sync(eng)
            return eng.state()

    log.info("%s | %s | risk %.2f%% | %s", PLAN.name, "LIVE" if live else "DRY RUN",
             RISK_FRAC * 100, PLAN.verdict[:60] + "...")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _selftest() -> None:
    global SECRET
    SECRET = "s3cret"
    ok = parse(b'{"key":"s3cret","side":"buy","entry":2000,"sl":1990,"tp":2008.4}')
    assert ok["side"] == 1 and abs(ok["sl"] - 1990) < 1e-9
    for bad in (b'{"key":"x","side":"buy","entry":2000,"sl":1990,"tp":2008}',   # secret
                b'{"key":"s3cret","side":"buy","entry":2000,"sl":2010,"tp":2008}',  # stop side
                b'{"key":"s3cret","side":"sell","entry":2000,"sl":2010,"tp":2005}',  # tp side
                b'not json'):
        try:
            parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted: {bad!r}")
    eng = RiskEngine(PLAN.initial, PLAN.rules)
    eng.mark(PLAN.initial + PLAN.initial * PLAN.rules.daily_profit_cap)
    assert eng.budget() == 0.0, "profit cap must stop new risk"
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        if not SECRET:
            sys.exit("WEBHOOK_SECRET is empty -- set it in .env before running")
        serve(a.live, a.host, a.port)
