"""How much Savings Plan commitment to buy, from an hourly history of commitment-eligible on-demand spend.

A Savings Plan is a promise to pay `c` dollars every hour for 1 or 3 years. In return, compute usage is billed at a
discount `d`, so the plan covers on-demand spend up to L = c / (1 - d) each hour; spend above L is billed at
on-demand rates, and an hour that uses less than L still pays the full `c`.

    cost(L) = sum over hours h of [ L * (1 - d) + max(0, spend_h - L) ]

cost(L) is convex and piecewise linear in L with slope H * (1 - d) - #{h : spend_h > L}, so it is minimal where the
number of hours above L falls to (1 - d) * H. That is: **cover the d-quantile of hourly spend** (with a 28%
discount, the level that 72% of hours exceed). It is the newsvendor problem: one more dollar of commitment saves
`d` in the hours that use it and wastes `1 - d` in the hours that don't.

Committing at the average, a common rule of thumb, over-commits whenever usage has peaks; committing at the minimum
leaves most of the saving on the table. `recommend` prices all three on the same history.
"""
from __future__ import annotations

import csv
import math
import random
from datetime import UTC, datetime, timedelta

# A typical discount for a 1-year, no-upfront Compute Savings Plan. It varies by instance family, region, term and
# payment option: take yours from the Savings Plans pricing page or Cost Explorer's recommendation.
DEFAULT_DISCOUNT = 0.28


def plan_cost(spend: list[float], level: float, discount: float) -> float:
    """Total cost over the history if the plan covers `level` dollars of on-demand spend per hour."""
    return sum(level * (1 - discount) + max(0.0, s - level) for s in spend)


def optimal_level(spend: list[float], discount: float) -> float:
    """The on-demand level to cover: the smallest sorted value with at most (1 - d) * H hours above it."""
    if not spend:
        return 0.0
    if not 0 < discount < 1:
        raise ValueError("discount must be between 0 and 1")
    s = sorted(spend)
    j = max(0, math.ceil(len(s) * discount) - 1)
    return s[j]


def _evaluate(spend: list[float], level: float, discount: float) -> dict:
    hours = len(spend)
    on_demand = sum(spend)
    covered = sum(min(s, level) for s in spend)
    total = plan_cost(spend, level, discount)
    return {
        "covered_on_demand_per_hour": round(level, 4),
        "commitment_per_hour": round(level * (1 - discount), 4),
        "cost": round(total, 2),
        "saving": round(on_demand - total, 2),
        "saving_pct": round((on_demand - total) / on_demand * 100, 1) if on_demand else 0.0,
        # utilisation: share of the commitment actually used; coverage: share of spend the plan covered
        "utilisation_pct": round(covered / (level * hours) * 100, 1) if level else 0.0,
        "coverage_pct": round(covered / on_demand * 100, 1) if on_demand else 0.0,
    }


def recommend(spend: list[float], discount: float = DEFAULT_DISCOUNT, drop: float = 0.2) -> dict:
    """The optimal commitment, the usual rules of thumb on the same history, and what happens if usage falls."""
    if not spend:
        raise ValueError("no usage history")
    hours = len(spend)
    best = optimal_level(spend, discount)
    on_demand = sum(spend)
    lower = [s * (1 - drop) for s in spend]
    return {
        "hours": hours,
        "discount": discount,
        "on_demand_cost": round(on_demand, 2),
        "monthly_factor": round(730 / hours, 4),          # multiply history totals by this for a monthly figure
        "optimal": _evaluate(spend, best, discount),
        "rules_of_thumb": {
            "commit at the minimum": _evaluate(spend, min(spend), discount),
            "commit at the median": _evaluate(spend, sorted(spend)[hours // 2], discount),
            "commit at the average": _evaluate(spend, on_demand / hours, discount),
        },
        # buying is for 1-3 years: show what the recommended commitment does if usage then falls
        "if_usage_falls": {"drop_pct": round(drop * 100), **_evaluate(lower, best, discount),
                           "on_demand_cost": round(sum(lower), 2)},
    }


def load_csv(path: str) -> list[float]:
    """Hourly history: a CSV with a `spend` (or `on_demand_usd`) column, one row per hour, oldest first."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    col = next((c for c in ("spend", "on_demand_usd", "cost") if rows and c in rows[0]), None)
    if col is None:
        raise ValueError(f"{path}: needs a 'spend' column")
    return [float(r[col]) for r in rows]


def from_cost_explorer(session, days: int = 14, end: datetime | None = None) -> list[float]:
    """Hourly EC2 + Fargate + Lambda compute spend from Cost Explorer (hourly granularity must be enabled; it keeps
    14 days). Usage that is already covered by commitments isn't included, so this sizes the next purchase."""
    ce = session.client("ce", region_name="us-east-1")
    end = (end or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    out, token = [], None
    while True:
        kw = {"TimePeriod": {"Start": start.strftime(fmt), "End": end.strftime(fmt)}, "Granularity": "HOURLY",
              "Metrics": ["UnblendedCost"],
              "Filter": {"And": [
                  {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Compute Cloud - Compute",
                                                              "Amazon Elastic Container Service", "AWS Lambda"]}},
                  {"Dimensions": {"Key": "PURCHASE_TYPE", "Values": ["On Demand Instances"]}}]}}
        if token:
            kw["NextPageToken"] = token
        res = ce.get_cost_and_usage(**kw)
        out += [float(r["Total"]["UnblendedCost"]["Amount"]) for r in res["ResultsByTime"]]
        token = res.get("NextPageToken")
        if not token:
            return out


def sample_series(days: int = 30, seed: int = 7) -> list[float]:
    """A realistic hourly compute bill: a steady base, a business-hours bump on weekdays, a nightly batch, noise."""
    rnd = random.Random(seed)
    out = []
    for h in range(days * 24):
        hour, weekday = h % 24, (h // 24) % 7 < 5
        spend = 14.0                                                         # always-on services
        if weekday and 8 <= hour < 20:
            spend += 9.0 * math.sin(math.pi * (hour - 8) / 12)               # daytime traffic
        if hour in (1, 2, 3):
            spend += 6.0                                                     # nightly batch
        out.append(round(max(0.0, spend + rnd.gauss(0, 0.8)), 4))
    return out


def markdown(r: dict) -> str:
    m = r["monthly_factor"]

    def row(name, e):
        return (f"| {name} | ${e['commitment_per_hour']:.2f} | ${e['saving'] * m:,.0f} | {e['saving_pct']}% | "
                f"{e['utilisation_pct']}% | {e['coverage_pct']}% |")

    lines = [
        "# Savings Plan commitment", "",
        (f"{r['hours']} hours of history, on-demand ${r['on_demand_cost'] * m:,.0f}/month, plan discount "
        f"{r['discount']:.0%}."), "",
        "| Commitment | $ per hour | Saving / month | Saving | Utilisation | Coverage |", "|---|---|---|---|---|---|",
        row("**optimal (d-quantile)**", r["optimal"]),
    ]
    lines += [row(k, v) for k, v in r["rules_of_thumb"].items()]
    f = r["if_usage_falls"]
    lines += ["", (f"If usage then falls {f['drop_pct']}%, the optimal commitment still saves "
                  f"${f['saving'] * m:,.0f}/month ({f['saving_pct']}%) at {f['utilisation_pct']}% utilisation."), "",
              ("Utilisation below 100% means some hours pay for commitment they don't use; that is part of the "
              "optimum, not a mistake. Buy in steps (for example half now, the rest next quarter) when usage is "
              "still changing.")]
    return "\n".join(lines) + "\n"
