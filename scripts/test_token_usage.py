"""Unit tests for the peak-demand logic in token_usage.py.

These cover the pure helpers (interval parsing, series peak/merge) and the
account/model/subscription peak aggregation in collect_usage, plus the optional
PTU-hint bridge into ptu_core. Azure is never called: collect_usage's per-account
fetch is monkeypatched with synthetic metrics.
"""

import datetime as dt

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


def test_demo_fetch_returns_expected_shape_and_no_private_keys():
    account = tu._DEMO_ACCOUNTS[0]
    clean, deployments, series = tu._demo_fetch(
        account, "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", "PT1H", None
    )
    # The "deployments" profile list must not leak into the report account dict.
    assert "deployments" not in clean
    assert clean["name"] == account["name"]
    assert set(deployments) == {d["name"] for d in account["deployments"]}
    for name, dep in deployments.items():
        assert dep["totals"]["total_tokens"] > 0
        assert dep["peak"]["tokens"] > 0
        assert series[name]  # non-empty timestamp series


def test_demo_collect_usage_produces_peaks_without_azure():
    report = tu.collect_usage(
        tu._DEMO_ACCOUNTS, "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
        "PT1H", None, workers=2, fetch=tu._demo_fetch,
    )
    assert len(report["accounts"]) == 2
    assert report["peak"]["tokens"] > 0
    # First account hosts two gpt-4.1 deployments -> a concurrent model peak exists.
    chat = next(a for a in report["accounts"] if a["name"] == "contoso-chat-eastus2")
    assert "gpt-4.1" in chat["model_peaks"]
    assert chat["model_peaks"]["gpt-4.1"]["tokens"] > 0


def test_demo_finer_interval_reveals_higher_peak_rate():
    """PT5M should surface a higher tokens/min peak than PT1H (intra-hour burst)."""
    args = ("2026-06-01T00:00:00Z", "2026-06-05T00:00:00Z")

    def peak_rate(interval):
        rep = tu.collect_usage(
            tu._DEMO_ACCOUNTS, *args, interval, None, workers=2, fetch=tu._demo_fetch
        )
        return rep["peak"]["tokens"] / tu._interval_minutes(interval)

    assert peak_rate("PT5M") > peak_rate("PT1H")



def test_parse_iso_accepts_z_and_offset():
    d = tu._parse_iso("2026-06-01T00:00:00Z")
    assert d == dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    # Naive timestamps are assumed UTC.
    assert tu._parse_iso("2026-06-01T00:00:00").tzinfo == dt.timezone.utc
    # Unparseable input returns None.
    assert tu._parse_iso("not-a-date") is None
    assert tu._parse_iso("") is None


def test_enforce_retention_passes_recent_start():
    now = dt.datetime(2026, 6, 24, tzinfo=dt.timezone.utc)
    start = "2026-06-01T00:00:00Z"  # 23 days back, within retention
    out, note = tu._enforce_retention(start, now)
    assert out == start
    assert note is None


def test_enforce_retention_warns_for_old_start():
    now = dt.datetime(2026, 6, 24, tzinfo=dt.timezone.utc)
    start = "2026-01-01T00:00:00Z"  # ~174 days back, beyond ~93-day retention
    out, note = tu._enforce_retention(start, now)
    assert out == start  # unchanged when only warning
    assert note is not None and "WARNING" in note


def test_enforce_retention_clamps_when_requested():
    now = dt.datetime(2026, 6, 24, tzinfo=dt.timezone.utc)
    start = "2026-01-01T00:00:00Z"
    out, note = tu._enforce_retention(start, now, clamp=True)
    cutoff = now - dt.timedelta(days=tu._METRIC_RETENTION_DAYS)
    assert out == cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert note is not None and "clamped" in note

