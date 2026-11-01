"""Savings Plan commitment: the d-quantile rule against brute force, the rules of thumb, Cost Explorer paging."""
import datetime as dt
import random

import boto3
import pytest
from botocore.stub import ANY, Stubber

from costopt import cli, commit


def brute_force_best(spend, d):
    """cost(L) is convex and piecewise linear with breakpoints at the spend values, so the optimum is one of them."""
    return min(commit.plan_cost(spend, level, d) for level in [0.0, *spend])


@pytest.mark.parametrize("seed", range(40))
def test_quantile_rule_is_optimal(seed):
    rnd = random.Random(seed)
    spend = [round(rnd.uniform(0, 50) * rnd.choice([0.2, 1, 1, 3]), 2) for _ in range(rnd.randint(1, 200))]
    d = rnd.choice([0.1, 0.28, 0.4, 0.66])
    level = commit.optimal_level(spend, d)
    assert commit.plan_cost(spend, level, d) == pytest.approx(brute_force_best(spend, d))


def test_flat_usage_commits_everything():
    r = commit.recommend([10.0] * 100, discount=0.3)
    assert r["optimal"]["covered_on_demand_per_hour"] == 10.0
    assert r["optimal"]["utilisation_pct"] == 100.0 and r["optimal"]["saving_pct"] == 30.0


def test_optimum_beats_every_rule_of_thumb_on_peaky_usage():
    r = commit.recommend(commit.sample_series(), discount=0.28)
    best = r["optimal"]["saving"]
    for name, alt in r["rules_of_thumb"].items():
        assert best >= alt["saving"], name
    # peaks above the base: committing at the average buys commitment the night hours can't use
    assert r["rules_of_thumb"]["commit at the average"]["utilisation_pct"] < r["optimal"]["utilisation_pct"]


def test_the_optimum_covers_hours_exceeded_by_1_minus_d_of_the_time():
    spend = list(range(1, 101))                      # 1..100
    level = commit.optimal_level(spend, 0.28)
    assert sum(s > level for s in spend) <= 72 <= sum(s >= level for s in spend)


def test_bad_inputs():
    with pytest.raises(ValueError):
        commit.optimal_level([1.0], 1.5)
    with pytest.raises(ValueError):
        commit.recommend([])


def test_cost_explorer_paging_and_filter():
    session = boto3.Session(aws_access_key_id="x", aws_secret_access_key="x", region_name="us-east-1")
    ce = session.client("ce", region_name="us-east-1")
    page = lambda amounts, token=None: {**({"NextPageToken": token} if token else {}), "ResultsByTime": [
        {"TimePeriod": {"Start": "a", "End": "b"}, "Total": {"UnblendedCost": {"Amount": str(a), "Unit": "USD"}},
         "Estimated": False} for a in amounts]}
    with Stubber(ce) as stub:
        stub.add_response("get_cost_and_usage", page([1.5, 2.5], token="t1"))
        stub.add_response("get_cost_and_usage", page([3.0]), {
            "TimePeriod": {"Start": "2026-10-31T00:00:00Z", "End": "2026-11-01T00:00:00Z"}, "Granularity": "HOURLY",
            "Metrics": ["UnblendedCost"], "NextPageToken": "t1", "Filter": ANY})

        class S:
            def client(self, *_a, **_k):
                return ce
        out = commit.from_cost_explorer(S(), days=1, end=dt.datetime(2026, 11, 1, tzinfo=dt.UTC))
    assert out == [1.5, 2.5, 3.0]


def test_cli_sample_markdown(tmp_path, capsys):
    out = tmp_path / "plan.md"
    assert cli.main(["commit", "--usage", "sample", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "optimal (d-quantile)" in text and "commit at the average" in text


def test_cli_csv(tmp_path):
    f = tmp_path / "usage.csv"
    f.write_text("hour,spend\n" + "".join(f"{i},{10 + (i % 24 > 12) * 5}\n" for i in range(48)), encoding="utf-8")
    out = tmp_path / "r.json"
    assert cli.main(["commit", "--usage", str(f), "--format", "json", "--out", str(out)]) == 0
    assert '"optimal"' in out.read_text(encoding="utf-8")
