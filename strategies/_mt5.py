"""MetaTrader 5 execution, gated by the risk engine.

    from strategies._mt5 import MT5Broker
    with MT5Broker() as b:                     # dry_run=True by default
        b.sync(engine)                         # account state -> RiskEngine
        b.place(engine, "EURUSD", side=+1, stop_distance=0.004)

**The workflow this is built for**, chosen from what was measured rather than what is
popular (`docs/PLAN.md`):

    daily bar close -> 60-120d trend signal on ~8 instruments -> vol-target sizing
    -> RiskEngine.size() -> ONE market order with a hard stop -> mark-to-market intraday

It is deliberately NOT an intraday/scalp loop. At 15m holds the cost drag alone is
-1.9 to -7.6 Sharpe against a break-even of -0.39, and the rapid-trading study measured
all six short-horizon variants as significantly negative with net EV equal to the
transaction cost. Trading faster is the one change guaranteed to lose money.

Three safety properties, in order of how much they matter:

  1. **dry_run is the default.** Orders are logged, not sent, until you pass
     dry_run=False explicitly. There is no config file that can flip this by accident.
  2. **Every order is sized by RiskEngine and requires a stop.** `place()` refuses a
     position with no stop distance, because an unstopped position cannot be risk-sized
     and one gap ends the account.
  3. **Account state is read from the broker, never assumed.** `sync()` pulls real
     equity into the engine, so a manual trade or an overnight swap cannot leave the
     engine's view of the account stale.

Credentials come from the environment — MT5_LOGIN, MT5_PASSWORD, MT5_SERVER — and are
never logged, never defaulted, and never written to disk by this module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEVIATION = 20          # max slippage in points the broker may fill within
MAGIC = 20260819        # tags our orders so manual ones are never touched
ACCOUNT_CCY = "USD"     # the currency loss_per_lot must answer in


@dataclass
class Order:
    """What was sent, or would have been sent in dry run."""

    symbol: str
    side: int                # +1 long, -1 short
    lots: float
    price: float
    stop_price: float
    risk: float              # currency at risk if the stop fills
    sent: bool
    result: object | None = None


class MT5Broker:
    def __init__(self, dry_run: bool = True, terminal_path: str | None = None):
        self.dry_run = dry_run
        self.terminal_path = terminal_path
        self._mt5 = None
        self._connected = False

    # ---- lifecycle --------------------------------------------------------
    def __enter__(self) -> "MT5Broker":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    def connect(self) -> None:
        """Attach to the local terminal, authorising it if it is not already.

        Credentials go to initialize() rather than only to login(), because a terminal
        with no account attached fails initialize() outright with (-6, 'Authorization
        failed') and login() is never reached. Passing them up front lets the API
        authorise the terminal itself, which is the difference between "works on a
        machine where someone already logged in by hand" and "works".
        """
        import MetaTrader5 as mt5

        self._mt5 = mt5
        login = os.environ.get("MT5_LOGIN")
        password = os.environ.get("MT5_PASSWORD")
        server = os.environ.get("MT5_SERVER")

        # 180s, not the 60s default: on a small VPS the terminal is still finishing its
        # own login when initialize() asks for the IPC channel, and the wait expires as
        # (-10005, 'IPC timeout') -- which reads like a network fault and is not one.
        kw = {"timeout": int(os.environ.get("MT5_TIMEOUT", "180000"))}
        if self.terminal_path:
            kw["path"] = self.terminal_path
        if login and password and server:
            kw.update(login=int(login), password=password, server=server)
        if not mt5.initialize(**kw):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

        # initialize() may attach to an already-running terminal on a DIFFERENT
        # account and still report success, so the login is asserted explicitly.
        if login and password and server:
            acc = mt5.account_info()
            if acc is None or acc.login != int(login):
                if not mt5.login(int(login), password=password, server=server):
                    mt5.shutdown()
                    raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        # else: use whatever account the running terminal is already attached to

        self._connected = True
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError("MT5 connected but account_info() is None")
        logger.info("MT5 %s account %s, balance %.2f %s, %s",
                    "DRY RUN" if self.dry_run else "LIVE",
                    acc.login, acc.balance, acc.currency, acc.server)

    def disconnect(self) -> None:
        if self._connected and self._mt5 is not None:
            self._mt5.shutdown()
            self._connected = False

    # ---- account ----------------------------------------------------------
    def account(self):
        acc = self._mt5.account_info()
        if acc is None:
            raise RuntimeError("account_info() returned None — terminal disconnected?")
        return acc

    def sync(self, engine) -> float:
        """Push real broker equity into the engine.

        Called before every sizing decision. The engine must never run on an assumed
        balance: a manual trade, a swap charge or a partial fill all move equity without
        the engine seeing it, and the daily limit is measured on equity.
        """
        eq = float(self.account().equity)
        engine.mark(eq)
        return eq

    # ---- symbols ----------------------------------------------------------
    def resolve(self, symbol: str) -> str:
        """Find the broker's name for a symbol.

        Brokers suffix instruments in their own way — EURUSD, EURUSD.raw, EURUSDm,
        EURUSD.pro. Guessing wrong means orders silently fail, so the exact match is
        tried first and any unique suffixed variant second.
        """
        mt5 = self._mt5
        if mt5.symbol_info(symbol) is not None:
            mt5.symbol_select(symbol, True)
            return symbol
        candidates = [s.name for s in (mt5.symbols_get() or [])
                      if s.name.upper().startswith(symbol.upper())]
        if not candidates:
            raise ValueError(f"symbol {symbol!r} not offered by this broker")
        if len(candidates) > 1:
            candidates.sort(key=len)
        chosen = candidates[0]
        mt5.symbol_select(chosen, True)
        logger.info("resolved %s -> %s", symbol, chosen)
        return chosen

    def price(self, symbol: str, side: int) -> float:
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"no tick for {symbol}")
        return float(tick.ask if side > 0 else tick.bid)

    def loss_per_lot(self, symbol: str, stop_distance: float) -> float:
        """Currency lost per 1.00 lot if price moves `stop_distance` (a FRACTION).

        Two independent routes, and the LARGER wins.

        The tick route (move / tick_size * tick_value) is normally right, and it is the
        only one that handles currency conversion for free: USDJPY reports tick_value
        0.626 and USDCHF 1.238, which are already expressed in the account currency.

        But a broker can report tick values that are simply wrong. Measured on
        FundingPips 2026-08-31: XAUUSD reports tick_value 0.01 against tick_size 0.01,
        a ratio of 1, when contract_size is 100. The tick route understated loss per lot
        by exactly 100x, the engine sized gold 100x too large -- 2.46 lots and $1.09M of
        notional on a $100k account -- and a 0.14% move cost $1,517 against a $372
        budget. Only the day-stop kept that inside the daily limit.

        So the contract route is computed too and the conservative answer is used. Being
        too small costs a smaller position; being too large costs the account.
        """
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise ValueError(f"no symbol_info for {symbol}")
        px = self.price(symbol, +1)
        move = px * stop_distance                      # price units
        if info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
            raise ValueError(f"{symbol}: broker reports no tick value")
        by_tick = (move / info.trade_tick_size) * info.trade_tick_value

        by_contract = self._contract_loss(info, px, move)
        if by_contract is None:
            return by_tick
        if by_contract > by_tick * 1.5:
            logger.warning(
                "%s: tick values understate risk %.0fx (tick %.2f vs contract %.2f per "
                "lot) — using the larger. tick_value=%s tick_size=%s contract=%s",
                symbol, by_contract / by_tick, by_tick, by_contract,
                info.trade_tick_value, info.trade_tick_size, info.trade_contract_size)
        return max(by_tick, by_contract)

    @staticmethod
    def _contract_loss(info, px: float, move: float) -> float | None:
        """Loss per lot from contract size, or None when a cross rate would be needed.

        Order matters: XAUUSD reports BOTH legs as USD, so `currency_base` must be
        checked AFTER `currency_profit` or gold is priced without its own price — wrong
        by a factor of the gold price.
        """
        contract = getattr(info, "trade_contract_size", 0.0)
        if contract <= 0:
            return None
        profit = getattr(info, "currency_profit", "")
        base = getattr(info, "currency_base", "")
        if profit == ACCOUNT_CCY:                  # EURUSD, XAUUSD: P&L already in USD
            return contract * move
        if base == ACCOUNT_CCY:                    # USDJPY: convert quote ccy at px
            return contract * move / px if px > 0 else None
        return None                                # a genuine cross: trust the ticks

    def filling(self, symbol: str) -> int:
        """The fill policy this SYMBOL accepts, not the one we prefer.

        `filling_mode` is a bitmask of SYMBOL_FILLING_*, and a broker that does not
        offer the mode you send rejects the order outright with "Unsupported filling
        mode" -- which is how six of eight orders died on the first live run while the
        two that happened to match went through and took the whole risk budget.
        """
        mt5 = self._mt5
        flags = getattr(self._mt5.symbol_info(symbol), "filling_mode", 0) or 0
        if flags & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            return mt5.ORDER_FILLING_FOK
        if flags & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            return mt5.ORDER_FILLING_IOC
        return getattr(mt5, "ORDER_FILLING_RETURN", mt5.ORDER_FILLING_IOC)

    def round_lots(self, symbol: str, lots: float) -> float:
        info = self._mt5.symbol_info(symbol)
        step = info.volume_step or 0.01
        lots = round(round(lots / step) * step, 8)
        return max(min(lots, info.volume_max), 0.0) if lots >= info.volume_min else 0.0

    # ---- the only method that can lose money -------------------------------
    def place(self, engine, symbol: str, side: int, stop_distance: float,
              fraction: float = 1.0, comment: str = "") -> Order | None:
        """Size from the engine and send one market order WITH a stop.

        Returns None when the engine refuses — that is the normal, expected outcome
        when a limit binds, not an error.
        """
        if side not in (1, -1):
            raise ValueError("side must be +1 or -1")
        if stop_distance <= 0:
            raise ValueError("stop_distance must be positive: an unstopped position "
                             "cannot be risk-sized")

        self.sync(engine)
        if not engine.can_trade():
            logger.info("engine refuses new risk: %s", engine.state())
            return None

        sym = self.resolve(symbol)
        risk = engine.budget() * min(max(fraction, 0.0), 1.0)
        per_lot = self.loss_per_lot(sym, stop_distance)
        lots = self.round_lots(sym, risk / per_lot) if per_lot > 0 else 0.0
        if lots <= 0:
            logger.info("%s: risk %.2f too small for one lot step", sym, risk)
            return None

        px = self.price(sym, side)
        stop_price = px * (1 - stop_distance) if side > 0 else px * (1 + stop_distance)
        actual_risk = lots * per_lot

        order = Order(symbol=sym, side=side, lots=lots, price=px,
                      stop_price=stop_price, risk=actual_risk, sent=False)

        if self.dry_run:
            logger.info("DRY RUN %s %s %.2f lots @ %.5f stop %.5f risk %.2f",
                        "BUY" if side > 0 else "SELL", sym, lots, px, stop_price,
                        actual_risk)
            engine.commit(actual_risk)
            return order

        mt5 = self._mt5
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": lots,
            "type": mt5.ORDER_TYPE_BUY if side > 0 else mt5.ORDER_TYPE_SELL,
            "price": px,
            "sl": stop_price,                      # never send an order without this
            "deviation": DEVIATION,
            "magic": MAGIC,
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self.filling(sym),
        }
        res = mt5.order_send(req)
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        order.sent, order.result = ok, res
        if ok:
            engine.commit(actual_risk)
            logger.info("FILLED %s %.2f lots @ %.5f", sym, lots, res.price)
        else:
            logger.error("order REJECTED %s: %s", sym,
                         getattr(res, "comment", None) or mt5.last_error())
        return order

    # ---- positions --------------------------------------------------------
    def positions(self) -> list:
        """Only positions this system opened — manual trades are left alone.

        Everything that ACTS in bulk uses this: close_all, adopt, the rebalance. A trade
        you placed by hand is yours and must never be closed by a rule you did not aim
        at it.
        """
        return [p for p in (self._mt5.positions_get() or []) if p.magic == MAGIC]

    def all_positions(self) -> list:
        """Every open position, ours or not. For DISPLAY and per-ticket action only.

        A hand-placed trade still moves equity and still counts against the daily limit,
        so hiding it from the cockpit makes the one number that matters unexplainable.
        Showing is not the same as closing: nothing bulk touches these.
        """
        return list(self._mt5.positions_get() or [])

    def modify(self, p, sl: float | None = None, tp: float | None = None) -> bool:
        """Change the stop and/or target of one open position.

        **A stop may be tightened, never widened or removed.** Moving a stop away as
        price approaches it is the single most reliable way retail accounts die, and
        docs/prop-firm-viability.md names it as a reason real pass rates sit far below
        the modelled ones. The engine sized this position assuming a stop distance; a
        wider stop silently makes it a bigger bet than the budget allowed.

        A target carries no such risk and is unrestricted.
        """
        mt5 = self._mt5
        is_long = p.type == mt5.POSITION_TYPE_BUY
        new_sl = float(p.sl) if sl is None else float(sl)
        new_tp = float(p.tp) if tp is None else float(tp)

        # order matters: removing a stop is its own failure and deserves its own words
        if new_sl <= 0:
            raise ValueError(f"{p.symbol}: refusing to leave a position unstopped")
        if sl is not None and p.sl:
            # tighter means closer to price: higher for a long, lower for a short
            tighter = new_sl >= p.sl if is_long else new_sl <= p.sl
            if not tighter:
                raise ValueError(
                    f"{p.symbol}: a stop may only be tightened. current {p.sl:.5f}, "
                    f"requested {new_sl:.5f} on a {'long' if is_long else 'short'}")

        if self.dry_run:
            logger.info("DRY RUN modify %s ticket %s sl %.5f tp %.5f",
                        p.symbol, p.ticket, new_sl, new_tp)
            return True
        res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol,
                              "position": p.ticket, "sl": new_sl, "tp": new_tp})
        ok = res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        if ok:
            logger.info("modified %s ticket %s -> sl %.5f tp %.5f",
                        p.symbol, p.ticket, new_sl, new_tp)
        else:
            logger.error("modify FAILED %s: %s", p.symbol,
                         getattr(res, "comment", None) or mt5.last_error())
        return ok

    def position_risk(self, p) -> float:
        """What a position still risks if its stop fills, from the BROKER's record.

        Used to release the right amount when one position is closed but others stay
        open. Re-deriving it from the current signal would drift, because the stop was
        set from the volatility of the day the position was opened.
        """
        info = self._mt5.symbol_info(p.symbol)
        sl = getattr(p, "sl", 0.0) or 0.0
        if info is None or sl <= 0 or info.trade_tick_size <= 0:
            return 0.0                          # unstopped or unknown: nothing to release
        move = abs(getattr(p, "price_open", 0.0) - sl)
        return move / info.trade_tick_size * info.trade_tick_value * p.volume

    def close(self, engine, p, reason: str = "") -> bool:
        """Close ONE position and release its risk. `close_all` is this in a loop.

        Closing one at a time is what keeps turnover low: a 120d signal flips on one
        instrument at a time, and flattening the whole book to re-open seven unchanged
        positions is the single most expensive mistake available (`docs/PLAN.md`).
        """
        mt5 = self._mt5
        side = -1 if p.type == mt5.POSITION_TYPE_BUY else +1
        if self.dry_run:
            logger.info("DRY RUN close %s %.2f lots (%s)", p.symbol, p.volume, reason)
        else:
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": p.ticket,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": mt5.ORDER_TYPE_SELL if side < 0 else mt5.ORDER_TYPE_BUY,
                "price": self.price(p.symbol, side),
                "deviation": DEVIATION,
                "magic": MAGIC,
                "comment": reason[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self.filling(p.symbol),
            }
            res = mt5.order_send(req)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error("close FAILED %s: %s", p.symbol, mt5.last_error())
                return False
        engine.release(self.position_risk(p))
        return True

    def close_all(self, engine, reason: str = "kill switch") -> int:
        """Flatten everything this system opened. The kill switch's teeth."""
        closed = sum(self.close(engine, p, reason) for p in self.positions())
        engine.committed = 0.0                  # nothing of ours is open any more
        engine.open_positions = 0
        return closed

    def guard(self, engine) -> bool:
        """Sync, and flatten immediately if a limit has been breached.

        Call this on every price update, not only around orders. A breach detected an
        hour late is a breach.
        """
        self.sync(engine)
        if engine.breached is not None:
            logger.error("BREACH (%s) — flattening", engine.breached)
            self.close_all(engine, reason=f"breach:{engine.breached}")
            return False
        # Intraday de-risk. Measured as the most valuable rule available: it collapsed
        # daily-limit failures from 64.1% to 7.7%. It only works if positions are
        # actually closed when it fires, which is what this does — the engine blocking
        # NEW risk is not enough when the loss is on an OPEN position.
        if engine.day_stopped and engine.rules.day_stop_residual <= 0.0:
            if self.positions():
                logger.warning("day-stop at -%.2f%% — flattening for the session",
                               engine.rules.day_stop * 100)
                self.close_all(engine, reason="day-stop")
            return False
        return True
