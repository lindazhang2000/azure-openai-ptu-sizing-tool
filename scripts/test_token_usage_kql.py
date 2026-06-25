import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import token_usage_kql as kql


def test_iso_to_kql_timespan_converts_common_intervals():
    assert kql._iso_to_kql_timespan("PT1H") == "1h"
    assert kql._iso_to_kql_timespan("PT5M") == "5m"
    assert kql._iso_to_kql_timespan("P1D") == "1d"
    assert kql._iso_to_kql_timespan("") == "1h"  # default


def test_build_kql_contains_metrics_window_and_bin():
    q = kql.build_kql("2026-06-01T00:00:00Z", "2026-06-25T00:00:00Z", "PT5M")
    assert "AzureMetrics" in q
    assert "datetime(2026-06-01T00:00:00Z)" in q
    assert "datetime(2026-06-25T00:00:00Z)" in q
    assert "bin(TimeGenerated, 5m)" in q
    for metric in ("ProcessedPromptTokens", "GeneratedTokens", "TokenTransaction"):
        assert metric in q
    assert 'MICROSOFT.COGNITIVESERVICES' in q


def test_compact_collapses_multiline_query():
    one_line = kql._compact("AzureMetrics\n| where x\n| project y")
    assert "\n" not in one_line
    assert one_line == "AzureMetrics | where x | project y"


def _sample_rows():
    rid = ("/subscriptions/s/resourceGroups/rg-demo/providers/"
           "Microsoft.CognitiveServices/accounts/acct1")
    return [
        {"ResourceId": rid, "Resource": "ACCT1", "MetricName": "ProcessedPromptTokens",
         "Total": 600, "Bucket": "2026-06-01T00:00:00Z"},
        {"ResourceId": rid, "Resource": "ACCT1", "MetricName": "GeneratedTokens",
         "Total": 400, "Bucket": "2026-06-01T00:00:00Z"},
        {"ResourceId": rid, "Resource": "ACCT1", "MetricName": "TokenTransaction",
         "Total": 1000, "Bucket": "2026-06-01T00:00:00Z"},
        {"ResourceId": rid, "Resource": "ACCT1", "MetricName": "TokenTransaction",
         "Total": 2500, "Bucket": "2026-06-01T01:00:00Z"},
    ]


def test_rows_to_report_aggregates_totals_and_peak():
    report = kql.rows_to_report(
        _sample_rows(), "2026-06-01T00:00:00Z", "2026-06-01T02:00:00Z", "PT1H", "gpt-4.1"
    )
    assert report["source"] == "log-analytics"
    assert len(report["accounts"]) == 1
    acc = report["accounts"][0]
    assert acc["name"] == "acct1"
    assert acc["resourceGroup"] == "rg-demo"
    totals = acc["totals"]
    assert totals["prompt_tokens"] == 600
    assert totals["generated_tokens"] == 400
    # total_tokens summed across both buckets
    assert totals["total_tokens"] == 3500
    # peak is the busiest single bucket
    assert acc["peak"]["tokens"] == 2500
    assert acc["peak"]["time"] == "2026-06-01T01:00:00Z"
    # one pseudo-deployment labelled with the model
    dep = acc["deployments"]["acct1"]
    assert dep["model"] == "gpt-4.1"
    # subscription rollup matches
    assert report["totals"]["total_tokens"] == 3500
    assert report["peak"]["tokens"] == 2500


def test_rows_to_report_feeds_usage_to_sizing_bridge():
    import usage_to_sizing as u2s

    report = kql.rows_to_report(
        _sample_rows(), "2026-06-01T00:00:00Z", "2026-06-01T02:00:00Z", "PT1H", "gpt-4.1"
    )
    core = u2s.tu._ptu_core()
    assert core is not None
    period_minutes = u2s._period_minutes(report)
    interval_minutes = report["interval_minutes"]
    _, dep_name, dep = next(u2s._iter_deployments(report, None, None))
    values = u2s.usage_to_inputs(
        dep, period_minutes, interval_minutes, core=core, deployment_type="Global"
    )
    result = core.calculate(u2s._public_inputs(values))
    assert result["recommended_ptu"] >= core.DEFAULTS["min_ptu_commit"]


def test_demo_rows_produce_a_nonempty_report():
    rows = kql._demo_rows("2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", "PT1H")
    assert rows
    report = kql.rows_to_report(
        rows, "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z", "PT1H", "gpt-4.1"
    )
    assert report["accounts"]
    assert report["peak"]["tokens"] > 0
