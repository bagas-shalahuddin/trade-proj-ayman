"""Named prop-firm plan configurations, typed from actual contracts.

    from strategies._plans import PLANS
    engine = RiskEngine(PLANS["fundingpips_50k"].initial,
                        PLANS["fundingpips_50k"].rules)

Every field comes from a dashboard or contract, not from a template. Marketing pages
describe these plans in terms of the profit target and the account size; the analysis in
`docs/prop-firm-viability.md` found the ranking is almost the reverse:

    1. time limit        unlimited beats any deadline, and outweighs the target
    2. daily limit       dominates the total drawdown
    3. withdrawal floor  a low one banks profit before the account is lost
    4. profit target     matters least of the four

`verdict` records what the model concluded for each plan, so a future session does not
re-derive it — or worse, quietly assume the FTMO-style defaults still apply.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategies._risk import PropRules


@dataclass(frozen=True)
class Plan:
    name: str
    initial: float
    fee: float | None
    rules: PropRules
    payout_at: float | None       # profit share needed before a withdrawal
    split: float | None
    verdict: str


PLANS: dict[str, Plan] = {
    # ------------------------------------------------------------------ #
    "fundednext_trial_100k": Plan(
        name="FundedNext $100k trial, 14 days",
        initial=100_000.0,
        fee=0.0,
        rules=PropRules(
            profit_target=0.05,                     # $5,000
            daily_limit=0.05,                       # $5,000
            max_loss=0.10,                          # $10,000, STATIC
            trailing=False,
            min_trading_days=3,
            max_days=14,                            # THE binding rule
            max_day_share=1.0,
            day_stop=0.015,
        ),
        payout_at=None,
        split=None,
        verdict=(
            "14 days is the whole story (research/prop_pass.py, 2026-09-22). At the "
            "0.75% sizing that is optimal everywhere else, 80% of attempts TIME OUT "
            "rather than lose: P(pass) 19.6%. It peaks at 73.3% around 3% risk "
            "($3,000/trade), where the zero-edge control is already 54.8% -- three "
            "quarters of that is variance, not the strategy. Same rules without the "
            "deadline: 99.0% at 0.75%. Treat a trial as a plumbing test, not a "
            "strategy test, and size at 3% knowingly."
        ),
    ),
    # ------------------------------------------------------------------ #
    "e8_pro_5k": Plan(
        name="E8 Pro $5k, 8% static (Forex market, 1:30)",
        initial=5_000.0,
        fee=24.0,                                   # promo; $32 list
        rules=PropRules(
            profit_target=0.08,                     # $400
            daily_limit=0.025,                      # $125 from today's start equity
            max_loss=0.08,                          # $400, STATIC
            trailing=False,
            min_trading_days=0,
            max_days=None,                          # unlimited
            max_day_share=1.0,                      # no consistency rule on E8 Pro
            daily_profit_cap=0.02,                  # $100/day counts; forces >= 4 days
            day_stop=0.015,                         # $75: measured as the best rule available
            # one trade at a time is enforced by deploy/ayman_e8/server.py, not here
        ),
        payout_at=0.01,
        split=0.80,
        verdict=(
            "Ayman M30 + relvol >= 0.99 filter (research/prop_pass.py, 2026-09-20): "
            "P(pass) 79-97% at 0.75% risk, median 48 trading days, bust 2.6%. The "
            "FUNDED stage is worth ~$90-230 once, then dies: E8 locks the loss floor at "
            "breakeven on the first payout. Run this for the live track record, not "
            "the payout; FTMO is where the same strategy is worth thousands."
        ),
    ),
    # ------------------------------------------------------------------ #
    "fundingpips_50k": Plan(
        name="FundingPips $50k evaluation",
        initial=50_000.0,
        fee=None,                                   # not yet supplied
        rules=PropRules(
            profit_target=0.08,                     # $4,000
            daily_limit=0.05,                       # $2,500 from today's start equity
            max_loss=0.10,                          # $5,000, threshold $45,000 = STATIC
            trailing=False,
            min_trading_days=3,
            max_days=14,                            # THE binding rule
            max_day_share=1.0,                      # no consistency rule advertised
        ),
        payout_at=None,
        split=None,
        verdict=(
            "NOT VIABLE for the measured book. The 14-day limit moves optimal sizing "
            "from 1% to 6-7% daily vol, above the 5% daily limit, so ~40% of attempts "
            "die on one bad day. P(pass) 36.5% at zero edge. The FX+gold book runs "
            "0.37% daily vol, so complying would need ~16x leverage and the strategy "
            "becomes a levered coin flip. Use the trial server for plumbing only."
        ),
    ),
    # ------------------------------------------------------------------ #
    "fundingpips_100k": Plan(
        name="FundingPips $100k evaluation",
        initial=100_000.0,
        fee=None,
        rules=PropRules(
            profit_target=0.08,                     # $8,000
            daily_limit=0.05,                       # $5,000 from today's start equity
            max_loss=0.10,                          # $10,000, threshold $90,000 = STATIC
            trailing=False,
            min_trading_days=3,
            max_days=14,
            max_day_share=1.0,
        ),
        payout_at=None,
        split=None,
        verdict=(
            "Same rules as the 50k, doubled. Every conclusion is unchanged because the "
            "rules are percentages: P(pass) 36.5% at zero edge, optimum 5-7% daily vol "
            "against an engine that can only reach 0.83%. Run it to prove the machine, "
            "not to pass. Also the plan to mirror when practising on a plain demo "
            "server -- the rules travel, the broker does not."
        ),
    ),
    # ------------------------------------------------------------------ #
    "the5ers_a_10k": Plan(
        name="The5ers Plan A $10k (high-reward)",
        initial=10_000.0,
        fee=69.0,
        rules=PropRules(
            profit_target=0.10,                     # phase 1; phase 2 is 5%
            daily_limit=0.05,
            max_loss=0.10,
            trailing=False,
            min_trading_days=0,
            max_days=None,                          # unlimited — the decisive advantage
            max_day_share=1.0,
        ),
        payout_at=0.015,                            # $150 withdraw target
        split=0.80,
        verdict=(
            "BEST OF THOSE PRICED. P(pass) 45.0% at zero edge, sizing ~1% daily vol. "
            "Wins despite a HARDER 10% target purely because time is unlimited. The "
            "$150 (1.5%) withdrawal threshold is the other advantage: profit is banked "
            "long before the account is lost."
        ),
    ),
    # ------------------------------------------------------------------ #
    "the5ers_b_10k": Plan(
        name="The5ers Plan B $10k",
        initial=10_000.0,
        fee=59.0,
        rules=PropRules(
            profit_target=0.10,                     # phase 1; phase 2 is 6%
            daily_limit=0.04,                       # tighter, and this is what costs it
            max_loss=0.12,                          # more total room
            trailing=False,
            min_trading_days=0,
            max_days=None,
            max_day_share=1.0,
        ),
        payout_at=0.015,
        split=0.95,
        verdict=(
            "LOSES TO PLAN A at every skill level despite a bigger drawdown allowance, "
            "a higher split and a lower fee. Its 4% daily limit costs more than the "
            "extra 2% of total drawdown returns — the daily limit dominates."
        ),
    ),
}


def get(key: str) -> Plan:
    if key not in PLANS:
        raise KeyError(f"unknown plan {key!r}; have {sorted(PLANS)}")
    return PLANS[key]
