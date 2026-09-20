"""Prop-firm risk engine: the layer that makes breaching the rules impossible.

Twenty-two investigations established there is no reliable directional edge in this data.
`docs/prop-firm-viability.md` then established something more useful: the challenge is
decided far more by SIZING and RULE COMPLIANCE than by edge.

    zero edge, sizing 0.25% -> 2.00% daily vol:  P(pass) 0% -> 25.2%
    fixed 2% vol, Sharpe 0 -> 1.0:               P(pass) 25.2% -> 40.3%

Getting the size right was worth more than a full point of Sharpe. That is what this
module does, and unlike an edge it does not have to be discovered.

Design rule: **a breach should be arithmetically impossible, not unlikely.** Every
position is sized so that if its stop is hit, the loss still leaves the account inside
every limit. The engine refuses trades rather than hoping.

Four limits are enforced together, and the binding one wins:

  daily loss    a share of the day's STARTING equity, reset each session
  total loss    a share of the initial balance (static) or the peak (trailing)
  consistency   no single day may dominate total profit; this one killed the naive
                model in prop-firm-viability.md and is the reason a challenge that
                looked +EV at zero edge is actually -EV
  safety buffer only a fraction of each limit is ever spendable, because a stop is a
                request rather than a guarantee — gaps and slippage overshoot it

`safety` is the difference between a model and a system. Sizing to 100% of the daily
limit means one gap through a stop ends the account.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PropRules:
    """FTMO-style defaults. Check every one against the actual contract."""

    profit_target: float = 0.10
    daily_limit: float = 0.05      # of the day's starting equity
    max_loss: float = 0.10         # of initial balance, or of peak if trailing
    trailing: bool = False
    max_day_share: float = 0.40    # consistency: best day / total profit
    safety: float = 0.60           # spendable share of each limit
    # Below this share of the initial balance the engine stops rather than sizing an
    # ever-smaller position. Two reasons: real accounts have minimum trade sizes, and
    # geometric decay of the remaining room converges ON the floor in floating point,
    # so without a hard stop the guarantee holds in algebra and fails in arithmetic.
    min_risk_frac: float = 0.002
    # Days on which at least one trade was placed. PLAN-SPECIFIC: set it from the
    # contract, typically 0-10. Without tracking it, passed() reports a pass the firm
    # would not grant. At 0-3 the rule almost never binds, because reaching +10% at 2%
    # daily vol takes roughly (0.10/0.02)^2 ~ 25 days anyway — see main_min_days().
    min_trading_days: int = 3
    # Cap on simultaneous committed risk as a share of the initial balance, and on the
    # number of open positions. Firms cap exposure directly; more importantly, many
    # correlated positions are one position wearing several names.
    max_exposure_frac: float = 0.04
    max_positions: int = 5
    # Calendar days allowed for the phase; None means unlimited. This is the rule that
    # dominated the FundingPips analysis: with 14 days the optimal sizing moves from
    # 1% to 6-7% daily vol, which is ABOVE the 5% daily limit, so ~40% of attempts die
    # on a single bad day. The engine cannot fix that — but it must not pretend the
    # target is reachable when it is not. See feasibility().
    max_days: int | None = None
    # INTRADAY DE-RISK — measured as the single most valuable rule available.
    # Cut to `day_stop_residual` once the day is down `day_stop`. On the FundingPips
    # rules this took P(pass) from 28.0% to 40.8% at 4% daily vol and to 53.4% at 6%,
    # by collapsing daily-limit failures from 64.1% to 7.7%. It helped the RANDOM
    # control by as much as any signal (22.1% -> 33.0%), which is why it is here: it
    # is a risk rule, requiring no forecast. See research/challenge_search.py.
    # None disables it. Must sit inside the daily limit or it is dead code.
    day_stop: float | None = 0.015
    day_stop_residual: float = 0.0
    # E8 Pro: only this share of the initial balance counts toward the target each
    # day. Profit past it is wasted, so a new position after the cap is pure risk for
    # nothing. None disables it.
    daily_profit_cap: float | None = None

    def __post_init__(self) -> None:
        for name in ("profit_target", "daily_limit", "max_loss"):
            v = getattr(self, name)
            if not 0.0 < v < 1.0:
                raise ValueError(f"{name} must be in (0, 1), got {v}")
        # 1.0 is the honest way to say "this firm has no consistency rule": one day
        # may legitimately be 100% of the profit. Rejecting it forced a fake value.
        if not 0.0 < self.max_day_share <= 1.0:
            raise ValueError(f"max_day_share must be in (0, 1], got {self.max_day_share}")
        if not 0.0 < self.safety <= 1.0:
            raise ValueError(f"safety must be in (0, 1], got {self.safety}")
        if not 0.0 < self.min_risk_frac < self.max_loss:
            raise ValueError("min_risk_frac must be in (0, max_loss)")
        if self.min_trading_days < 0:
            raise ValueError("min_trading_days cannot be negative")
        if self.max_positions < 1:
            raise ValueError("max_positions must be at least 1")
        if not 0.0 < self.max_exposure_frac <= self.max_loss:
            raise ValueError("max_exposure_frac must be in (0, max_loss]")
        if self.max_days is not None and self.max_days < 1:
            raise ValueError("max_days must be at least 1, or None for unlimited")
        if self.day_stop is not None:
            if not 0.0 < self.day_stop < self.daily_limit:
                raise ValueError(
                    f"day_stop must be inside the daily limit (0, {self.daily_limit}); "
                    f"at or beyond it the daily rule fires first and this is dead code")
            if not 0.0 <= self.day_stop_residual < 1.0:
                raise ValueError("day_stop_residual must be in [0, 1)")
        if self.daily_limit > self.max_loss:
            raise ValueError("daily_limit above max_loss makes the daily rule dead code")


@dataclass
class RiskEngine:
    """Tracks account state and answers one question: how much may I risk right now?

    Currency units throughout — no percentages in the interface, because mixing the two
    is how a sizing bug becomes a blown account.
    """

    initial: float
    rules: PropRules = field(default_factory=PropRules)
    equity: float = field(init=False)
    day_start: float = field(init=False)
    peak: float = field(init=False)
    best_day: float = field(init=False, default=0.0)
    committed: float = field(init=False, default=0.0)   # risk on open positions
    open_positions: int = field(init=False, default=0)
    trading_days: int = field(init=False, default=0)
    days_elapsed: int = field(init=False, default=0)
    traded_today: bool = field(init=False, default=False)
    blackout: bool = field(init=False, default=False)
    day_stopped: bool = field(init=False, default=False)
    halted: bool = field(init=False, default=False)
    breached: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.initial <= 0:
            raise ValueError("initial balance must be positive")
        self.equity = self.day_start = self.peak = self.initial

    # ---- state transitions ------------------------------------------------
    def start_day(self) -> None:
        """New session: the daily allowance resets, the total allowance does not.

        `day_start` is EQUITY, not balance — floating P&L on positions held through the
        rollover counts against today from the moment the session opens. Firms that
        measure the daily limit on equity work exactly this way, and a position carried
        overnight can therefore start the day already partway into its allowance.
        """
        self.day_start = self.equity
        self.halted = False
        self.traded_today = False
        self.day_stopped = False        # a new session restores full size

    def mark(self, equity: float) -> None:
        """Mark to market. Call on every price update, not just on fills.

        This is where the intraday de-risk fires, which is why it must be called on
        price movement rather than only around orders: a stop that is evaluated once a
        day is not an intraday stop.
        """
        self.equity = equity
        self.peak = max(self.peak, equity)
        if (self.rules.day_stop is not None and not self.day_stopped
                and equity <= self.day_start * (1 - self.rules.day_stop)):
            self.day_stopped = True
        if equity <= self._total_floor():
            self.breached = "max_loss"
        elif equity <= self.day_start * (1 - self.rules.daily_limit):
            self.breached = "daily_loss"

    def close_day(self) -> None:
        """End of session: record the day's profit and whether it counted as a day."""
        self.best_day = max(self.best_day, self.equity - self.day_start)
        self.days_elapsed += 1
        if self.traded_today:
            self.trading_days += 1
        self.traded_today = False
        self.day_start = self.equity

    # ---- limits -----------------------------------------------------------
    def _total_floor(self) -> float:
        ref = self.peak if self.rules.trailing else self.initial
        return ref * (1 - self.rules.max_loss)

    def room_daily(self) -> float:
        """Currency the account may lose today before the daily rule triggers."""
        return max(self.equity - self.day_start * (1 - self.rules.daily_limit), 0.0)

    def room_total(self) -> float:
        return max(self.equity - self._total_floor(), 0.0)

    def consistency_throttle(self) -> float:
        """Multiplier in (0, 1] that discourages one day dominating total profit.

        Heuristic and deliberately so: the rule is judged on the FINAL distribution,
        which is unknowable mid-challenge. When today's gain is already a large share of
        total profit, size down — the fix for a lopsided profit curve is more modest
        days, not one more big one.
        """
        profit = self.equity - self.initial
        if profit <= 0:
            return 1.0
        biggest = max(self.best_day, self.equity - self.day_start)
        if biggest <= 0:
            return 1.0
        share = biggest / profit
        if share <= self.rules.max_day_share:
            return 1.0
        return max(self.rules.max_day_share / share, 0.25)

    # ---- the deadline ------------------------------------------------------
    def days_left(self) -> float:
        return float("inf") if self.rules.max_days is None else             max(self.rules.max_days - self.days_elapsed, 0)

    def expired(self) -> bool:
        return self.days_left() <= 0 and not self.at_target()

    def feasibility(self) -> dict:
        """Is the target still reachable, and at what volatility?

        Honest arithmetic rather than encouragement. Reaching a remaining return R over
        D days needs roughly R/D per day; expressed as the daily volatility a coin-flip
        strategy would need, that is R/(D * 0.4) — a driftless walk covers about 0.4
        sigma of net progress per day toward one barrier.

        When the required volatility exceeds the daily LOSS limit, the target cannot be
        chased safely: any sizing large enough to arrive is large enough to be stopped
        out by the daily rule first. The engine reports this instead of quietly letting
        fees bleed away.
        """
        need = self.initial * (1 + self.rules.profit_target) - self.equity
        if need <= 0:
            return {"reachable": True, "required_vol": 0.0, "days_left": self.days_left()}
        d = self.days_left()
        if d == float("inf"):
            return {"reachable": True, "required_vol": 0.0, "days_left": d}
        if d <= 0:
            return {"reachable": False, "required_vol": float("inf"), "days_left": 0}
        req_vol = (need / self.initial) / (d * 0.4)
        return {"reachable": req_vol <= self.rules.daily_limit,
                "required_vol": req_vol, "days_left": d}

    # ---- progress toward passing ------------------------------------------
    def at_target(self) -> bool:
        return self.equity >= self.initial * (1 + self.rules.profit_target)

    def days_remaining(self) -> int:
        """Trading days still required, counting today if it has seen a trade."""
        done = self.trading_days + (1 if self.traded_today else 0)
        return max(self.rules.min_trading_days - done, 0)

    def passed(self) -> bool:
        """Both conditions, not just the profit one."""
        return (self.breached is None and self.at_target()
                and self.days_remaining() == 0)

    def coasting(self) -> bool:
        """At target, but the day requirement is not yet met.

        The naive move is to stop trading, which never satisfies the rule. The correct
        move is to keep placing MINIMUM-size trades: each ticks a day off without
        putting the earned profit back at risk. That is what `budget` returns here.
        """
        return self.at_target() and self.days_remaining() > 0

    # ---- the interface that matters ---------------------------------------
    def can_trade(self) -> bool:
        return (self.breached is None and not self.halted and not self.blackout
                and not self.passed()
                and self.open_positions < self.rules.max_positions
                and self.budget() > 0.0)

    def budget(self) -> float:
        """Currency that may be put at risk on NEW positions, right now.

        The binding limit wins, a safety fraction is applied, and risk already committed
        to open positions is subtracted — otherwise ten trades each sized to the full
        remaining allowance would collectively breach it ten times over.
        """
        if self.breached is not None or self.halted or self.blackout or self.passed():
            return 0.0
        floor = self.initial * self.rules.min_risk_frac
        if self.day_stopped and self.rules.day_stop_residual <= 0.0:
            return 0.0                  # de-risked to flat: nothing more today
        if self.open_positions >= self.rules.max_positions:
            return 0.0
        if (self.rules.daily_profit_cap is not None
                and self.equity - self.day_start >= self.initial * self.rules.daily_profit_cap):
            return 0.0                  # today's profit no longer counts: stop adding risk
        # target met but days outstanding: risk the minimum, protect the profit
        if self.coasting():
            return floor if self.room_daily() > floor else 0.0

        allowance = min(self.room_daily(), self.room_total()) * self.rules.safety
        if self.day_stopped:
            allowance *= self.rules.day_stop_residual
        # exposure cap: total risk live at once, independent of the loss limits
        cap = self.initial * self.rules.max_exposure_frac
        free = min(allowance * self.consistency_throttle(), cap) - self.committed
        # too little room left to place a real trade: stop, do not shrink forever
        return free if free >= floor else 0.0

    def size(self, stop_distance: float, price: float = 1.0,
             fraction: float = 1.0) -> float:
        """Position size in units, such that a stop-out costs at most the budget.

        `stop_distance` is the fractional adverse move to the stop (e.g. 0.02 for 2%).
        `fraction` lets a caller take only part of the available budget, which is what
        you want when several positions will be open at once.
        """
        if stop_distance <= 0:
            raise ValueError("stop_distance must be positive — an unstopped position "
                             "cannot be risk-sized")
        if price <= 0:
            raise ValueError("price must be positive")
        risk = self.budget() * min(max(fraction, 0.0), 1.0)
        return risk / (stop_distance * price)

    def commit(self, risk: float) -> None:
        """Reserve risk for a position that has just been opened."""
        if risk < 0:
            raise ValueError("committed risk cannot be negative")
        self.committed += risk
        self.open_positions += 1
        self.traded_today = True          # this day now counts toward the minimum

    def release(self, risk: float) -> None:
        self.committed = max(self.committed - risk, 0.0)
        self.open_positions = max(self.open_positions - 1, 0)

    def set_blackout(self, on: bool) -> None:
        """News window, weekend, or any period the contract forbids new positions."""
        self.blackout = on

    def halt(self, reason: str = "manual") -> None:
        """Kill switch. Stops new positions without recording a rule breach."""
        self.halted = True

    # ---- reporting --------------------------------------------------------
    def state(self) -> dict:
        profit = self.equity - self.initial
        return {
            "equity": self.equity,
            "profit_pct": profit / self.initial,
            "room_daily_pct": self.room_daily() / self.initial,
            "room_total_pct": self.room_total() / self.initial,
            "budget": self.budget(),
            "throttle": self.consistency_throttle(),
            "can_trade": self.can_trade(),
            "breached": self.breached,
            "at_target": self.at_target(),
            "days_done": self.trading_days + (1 if self.traded_today else 0),
            "days_remaining": self.days_remaining(),
            "coasting": self.coasting(),
            "day_stopped": self.day_stopped,
            "days_left": self.days_left(),
            "feasibility": self.feasibility(),
            "open_positions": self.open_positions,
            "passed": self.passed(),
        }
