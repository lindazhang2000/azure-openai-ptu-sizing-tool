"""Unit tests for the peak-demand logic in token_usage.py.

These cover the pure helpers (interval parsing, series peak/merge) and the
account/model/subscription peak aggregation in collect_usage, plus the optional
PTU-hint bridge into ptu_core. Azure is never called: collect_usage's per-account
fetch is monkeypatched with synthetic metrics.
"""

import pytest

import token_usage as tu


def test_interval_minutes_parses_common_durations():
    assert tu._interval_minutes("PT1H") == 60
    assert tu._interval_minutes("PT5M") == 5
    assert tu._interval_minutes("P1D") == 1440
    assert tu._interval_minutes("PT1H30M") == 90
    assert tu._interval_minutes("PT30S") == pytest.approx(0.5)


def test_interval_minutes_handles_bad_input():
    assert tu._interval_minutes("") == 0.0
    assert tu._interval_minutes("garbage") == 0.0


def test_peak_of_series_returns_busiest_bucket():
    series = {"t1": 80.0, "t2": 150.0, "t3": 70.0}
    assert tu._peak_of_series(series) == ("t2", 150.0)


def test_peak_of_series_empty_is_zero():
    assert tu._peak_of_series({}) == ("", 0.0)


def test_merge_series_adds_in_place():
    target = {"t1": 10.0, "t2": 20.0}
    tu._merge_series(target, {"t2": 5.0, "t3": 3.0})
    assert target == {"t1": 10.0, "t2": 25.0, "t3": 3.0}


def _fake_fetch(account, start, end, interval, subscription):
    """Two deployments of the same model with overlapping timestamp series."""
    deployments = {
        "dep-a": {
            "model": "gpt-4.1", "version": "2025-04-14",
            "totals": {"prompt_tokens": 1000, "generated_tokens": 500, "total_tokens": 1500},
            "peak": {"tokens": 80, "time": "t1"},
        },
        "dep-b": {
            "model": "gpt-4.1", "version": "2025-04-14",
            "totals": {"prompt_tokens": 2000, "generated_tokens": 1000, "total_tokens": 3000},
            "peak": {"tokens": 90, "time": "t2"},
        },
    }
    series_by_dep = {
        "dep-a": {"t1": 80.0, "t2": 70.0},
        "dep-b": {"t1": 30.0, "t2": 90.0},
    }
    return account, deployments, series_by_dep


def test_collect_usage_aggregates_totals_and_peaks(monkeypatch):
    monkeypatch.setattr(tu, "_fetch_account_usage", _fake_fetch)
    accounts = [{"name": "acct1", "resourceGroup": "rg", "location": "swedencentral", "kind": "AIServices"}]

    report = tu.collect_usage(accounts, "s", "e", "PT1H", None, workers=1)

    acc = report["accounts"][0]
    # Totals are summed across deployments.
    assert acc["totals"]["total_tokens"] == 4500
    assert acc["totals"]["prompt_tokens"] == 3000

    # Account peak = busiest merged bucket: t1=110, t2=160 -> 160 at t2.
    assert acc["peak"] == {"tokens": 160, "time": "t2"}
    # Per-model concurrent peak merges both deployments of gpt-4.1.
    assert acc["model_peaks"]["gpt-4.1"] == {"tokens": 160, "time": "t2"}
    # Subscription peak rolls up the single account.
    assert report["peak"] == {"tokens": 160, "time": "t2"}
    assert report["totals"]["total_tokens"] == 4500
    assert report["interval_minutes"] == 60


def test_collect_usage_handles_empty_account(monkeypatch):
    monkeypatch.setattr(
        tu, "_fetch_account_usage",
        lambda account, *a, **k: (account, {}, {}),
    )
    report = tu.collect_usage([{"name": "empty"}], "s", "e", "PT1H", None, workers=1)
    assert report["accounts"][0]["peak"] == {"tokens": 0, "time": ""}
    assert report["peak"] == {"tokens": 0, "time": ""}


def test_deployment_ptu_hint_maps_peak_to_ptu():
    # gpt-4.1: model_tpm_per_ptu 3000, output_weight 4.0, min 15, inc 5.
    dep = {
        "model": "gpt-4.1",
        "totals": {"prompt_tokens": 3000, "generated_tokens": 3000, "total_tokens": 6000},
        "peak": {"tokens": 6000, "time": "t"},
    }
    # 6000 tokens / 60 min = 100/min; gen_frac 0.5 -> weight 0.5 + 0.5*4 = 2.5;
    # weighted 250 tpm; raw 250/3000=0.083; *1.15 -> round up 5 -> 5; floored at 15.
    hint = tu._deployment_ptu_hint(dep, 60)
    assert hint is not None
    assert hint["preset"] == "gpt-4.1"
    assert hint["ptu"] == 15
    assert hint["weighted_tpm"] == pytest.approx(250.0)


def test_deployment_ptu_hint_none_without_peak():
    dep = {"model": "gpt-4.1", "totals": {}, "peak": {"tokens": 0, "time": ""}}
    assert tu._deployment_ptu_hint(dep, 60) is None
    # Zero interval also yields no hint (avoids divide-by-zero).
    assert tu._deployment_ptu_hint({"model": "gpt-4.1", "peak": {"tokens": 10}}, 0) is None
