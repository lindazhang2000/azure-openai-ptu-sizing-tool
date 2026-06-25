"""Collect Azure OpenAI token usage from a Log Analytics workspace (KQL).

A sibling of [token_usage.py](token_usage.py) for teams whose token telemetry
already lands in **Log Analytics** rather than being read live from Azure Monitor.
When you point a Cognitive Services / Azure OpenAI account's **diagnostic setting**
at a Log Analytics workspace with *AllMetrics* enabled, the same token counters
show up in the ``AzureMetrics`` table:

    * ProcessedPromptTokens  -> input / prompt tokens
    * GeneratedTokens        -> output / completion tokens
    * TokenTransaction       -> total inference tokens (input + output)

This script runs a KQL query against the workspace (``az monitor log-analytics
query``), totals those metrics per resource, and finds the **peak demand** bucket
(busiest ``--interval``) for PTU sizing. It writes the result in the *same JSON
shape* as ``token_usage.py``, so it flows straight into the sizing bridge:

    python scripts/token_usage_kql.py -w <workspace-guid> --json usage.json
    python scripts/usage_to_sizing.py --from-json usage.json --calculate

Note on dimensions: platform metrics in ``AzureMetrics`` are pre-aggregated and do
**not** preserve the ``ModelDeploymentName`` dimension, so usage is reported per
*account* (one pseudo-deployment labelled with ``--model`` so a preset matches).
If you need a true per-deployment split, use ``token_usage.py`` (live Azure Monitor),
which queries the deployment dimension directly.

Usage:
    az login
    python scripts/token_usage_kql.py -w <workspace-guid>             # last 30 days, hourly
    python scripts/token_usage_kql.py -w <guid> --days 7 --interval PT5M
    python scripts/token_usage_kql.py -w <guid> --model gpt-4.1 --json usage.json --csv usage.csv
    python scripts/token_usage_kql.py --print-query                   # just print the KQL recipe
    python scripts/token_usage_kql.py --demo --ptu-hint               # synthetic, no Azure needed

Requires Azure credentials (``az login``) and **Log Analytics Reader** on the
workspace. The workspace id is its *customer/GUID* (``customerId``), e.g.
``az monitor log-analytics workspace show -g <rg> -n <ws> --query customerId -o tsv``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

import token_usage as tu

# Map the AzureMetrics MetricName -> the report's friendly column (same columns
# token_usage.py uses, so downstream tooling is identical).
_METRIC_COLUMN = {
    "ProcessedPromptTokens": "prompt_tokens",
    "GeneratedTokens": "generated_tokens",
    "TokenTransaction": "total_tokens",
}

# Default resource provider filter (Azure OpenAI / AI Services emit under this).
_RESOURCE_PROVIDER = "MICROSOFT.COGNITIVESERVICES"


def _iso_to_kql_timespan(interval_iso: str) -> str:
    """Convert an ISO 8601 duration (PT1H, PT5M, P1D) to a KQL timespan (1h, 5m, 1d)."""
    minutes = tu._interval_minutes(interval_iso) or 60.0
    if minutes % 1440 == 0:
        return f"{int(minutes // 1440)}d"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}h"
    return f"{int(minutes)}m"


def build_kql(start: str, end: str, interval_iso: str,
              resource_provider: str = _RESOURCE_PROVIDER) -> str:
    """Return the KQL that totals the token metrics per resource and time bucket.

    The query is intentionally simple and portable so it can also be pasted into
    the Log Analytics portal (``--print-query``). It bins by ``--interval`` so the
    busiest bucket gives the peak demand used for sizing.
    """
    bin_size = _iso_to_kql_timespan(interval_iso)
    metric_list = ", ".join(f'"{m}"' for m in _METRIC_COLUMN)
    return (
        "AzureMetrics\n"
        f"| where TimeGenerated >= datetime({start}) and TimeGenerated < datetime({end})\n"
        f'| where ResourceProvider == "{resource_provider}"\n'
        f"| where MetricName in ({metric_list})\n"
        f"| summarize Total = sum(Total) by ResourceId, Resource, MetricName, "
        f"Bucket = bin(TimeGenerated, {bin_size})\n"
        "| project ResourceId, Resource, MetricName, Total, Bucket\n"
        "| order by Bucket asc"
    )


def _compact(kql: str) -> str:
    """Collapse a multi-line KQL string to one line for safe CLI passing on Windows."""
    return " ".join(part.strip() for part in kql.splitlines() if part.strip())


def _rg_from_id(resource_id: str) -> str:
    return tu._rg_from_id(resource_id)


def _query_workspace(workspace: str, kql: str, subscription: str | None) -> list[dict]:
    """Run the KQL against the workspace via az CLI; return a list of row dicts."""
    args = [
        "monitor", "log-analytics", "query",
        "--workspace", workspace,
        "--analytics-query", _compact(kql),
    ]
    if subscription:
        args += ["--subscription", subscription]
    rows = tu._az(args) or []
    # az returns a list of objects keyed by column name (TableName column added).
    return rows if isinstance(rows, list) else []


def rows_to_report(rows: list[dict], start: str, end: str, interval_iso: str,
                   model: str) -> dict:
    """Assemble token_usage-shaped report JSON from flat KQL result rows.

    Each distinct ResourceId becomes an account with a single pseudo-deployment
    (labelled ``model`` so a sizing preset matches); ``AzureMetrics`` has no
    deployment dimension to split on.
    """
    # account_id -> {"name","resourceGroup","counts","series"(total_tokens by bucket)}
    accounts: dict[str, dict] = {}
    for row in rows:
        rid = row.get("ResourceId") or row.get("_ResourceId") or tu._UNKNOWN
        metric = row.get("MetricName")
        column = _METRIC_COLUMN.get(metric)
        if not column:
            continue
        try:
            total = float(row.get("Total") or 0.0)
        except (TypeError, ValueError):
            total = 0.0
        if total <= 0:
            continue
        bucket = row.get("Bucket") or row.get("TimeGenerated") or ""
        name = (row.get("Resource") or rid.split("/")[-1] or tu._UNKNOWN)

        acc = accounts.setdefault(rid, {
            "name": str(name).lower() if name else tu._UNKNOWN,
            "resourceGroup": _rg_from_id(rid),
            "counts": {c: 0.0 for c in _METRIC_COLUMN.values()},
            "series": {},
        })
        acc["counts"][column] += total
        if column == "total_tokens":
            acc["series"][bucket] = acc["series"].get(bucket, 0.0) + total

    interval_minutes = tu._interval_minutes(interval_iso)
    accounts_out: list[dict] = []
    grand_total = {c: 0.0 for c in _METRIC_COLUMN.values()}
    global_series: dict[str, float] = {}

    for acc in accounts.values():
        counts = {k: round(v) for k, v in acc["counts"].items()}
        peak_time, peak_val = tu._peak_of_series(acc["series"])
        dep = {
            "model": model,
            "version": tu._UNKNOWN,
            "totals": counts,
            "peak": {"tokens": round(peak_val), "time": peak_time},
        }
        for stamp, val in acc["series"].items():
            global_series[stamp] = global_series.get(stamp, 0.0) + val
        for c in grand_total:
            grand_total[c] += acc["counts"][c]
        accounts_out.append({
            "name": acc["name"],
            "resourceGroup": acc["resourceGroup"],
            "location": tu._UNKNOWN,
            "kind": "AIServices",
            "deployments": {acc["name"]: dep},
            "model_totals": {model: counts},
            "model_peaks": {model: {"tokens": round(peak_val), "time": peak_time}},
            "totals": counts,
            "peak": {"tokens": round(peak_val), "time": peak_time},
        })

    accounts_out.sort(key=lambda a: a["name"] or "")
    sub_peak_time, sub_peak_val = tu._peak_of_series(global_series)
    return {
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "log-analytics",
        "period": {"start": start, "end": end, "interval": interval_iso},
        "interval_minutes": interval_minutes,
        "metrics": list(_METRIC_COLUMN.keys()),
        "accounts": accounts_out,
        "totals": {k: round(v) for k, v in grand_total.items()},
        "peak": {"tokens": round(sub_peak_val), "time": sub_peak_time},
    }


# ---------------------------------------------------------------------------
# Demo mode — synthetic KQL rows so the tool runs with no workspace or creds.
# ---------------------------------------------------------------------------
def _demo_rows(start: str, end: str, interval_iso: str) -> list[dict]:
    """Build a small synthetic AzureMetrics rowset (one account, hourly buckets)."""
    minutes = tu._interval_minutes(interval_iso) or 60.0
    t0 = tu._parse_iso(start) or (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1))
    t1 = tu._parse_iso(end) or _dt.datetime.now(_dt.timezone.utc)
    step = _dt.timedelta(minutes=minutes)
    rid = ("/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/demo-rg"
           "/providers/Microsoft.CognitiveServices/accounts/contoso-chat-eastus2")
    rows: list[dict] = []
    t = t0
    n = 0
    while t < t1 and n < 5000:
        hour = t.hour + t.minute / 60.0
        import math
        diurnal = 0.5 * (1.0 + math.cos((hour - 14.0) / 24.0 * 2.0 * math.pi))
        total = 18000 + 42000 * diurnal
        prompt = total * 0.62
        generated = total - prompt
        bucket = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        for metric, val in (
            ("ProcessedPromptTokens", prompt),
            ("GeneratedTokens", generated),
            ("TokenTransaction", total),
        ):
            rows.append({
                "ResourceId": rid,
                "Resource": "CONTOSO-CHAT-EASTUS2",
                "MetricName": metric,
                "Total": round(val, 1),
                "Bucket": bucket,
            })
        t += step
        n += 1
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-w", "--workspace",
                        help="Log Analytics workspace customer/GUID id (required for a live query).")
    parser.add_argument("--subscription", help="Subscription ID/name to scope the az query.")
    parser.add_argument("--days", type=int, default=30,
                        help="Look back N days from now (default 30). Ignored if --start is set.")
    parser.add_argument("--start", help="Start time, ISO 8601 UTC (e.g. 2026-06-01T00:00:00Z).")
    parser.add_argument("--end", help="End time, ISO 8601 UTC (default now).")
    parser.add_argument("--interval", default="PT1H",
                        help="Bucket size (ISO 8601 duration, default PT1H). Peak demand is the "
                             "busiest single bucket of this size.")
    parser.add_argument("--model", default="gpt-4.1",
                        help="Model label for the per-account pseudo-deployment so a sizing "
                             "preset matches (default gpt-4.1). AzureMetrics has no model split.")
    parser.add_argument("--resource-provider", default=_RESOURCE_PROVIDER,
                        help=f"ResourceProvider filter (default {_RESOURCE_PROVIDER}).")
    parser.add_argument("--json", dest="json_path", help="Write the full breakdown to this JSON file.")
    parser.add_argument("--csv", dest="csv_path", help="Write a flat per-account CSV to this file.")
    parser.add_argument("--ptu-hint", action="store_true",
                        help="Show a directional baseline-PTU suggestion for each account's peak.")
    parser.add_argument("--print-query", action="store_true",
                        help="Print the KQL recipe (filled with the time window) and exit.")
    parser.add_argument("--demo", action="store_true",
                        help="Use built-in synthetic rows instead of querying a workspace.")
    args = parser.parse_args(argv)

    now = _dt.datetime.now(_dt.timezone.utc)
    end = args.end or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = args.start or (now - _dt.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start, note = tu._enforce_retention(start, now, clamp=True)
    if note:
        print(note, file=sys.stderr)

    kql = build_kql(start, end, args.interval, args.resource_provider)

    if args.print_query:
        print(kql)
        return 0

    if args.demo:
        print("DEMO MODE: synthetic Log Analytics rows, no Azure calls.", file=sys.stderr)
        rows = _demo_rows(start, end, args.interval)
    else:
        if not args.workspace:
            print("ERROR: --workspace (Log Analytics customer/GUID id) is required "
                  "for a live query. Use --print-query to see the KQL, or --demo.",
                  file=sys.stderr)
            return 2
        print("Querying Log Analytics workspace...", file=sys.stderr)
        try:
            rows = _query_workspace(args.workspace, kql, args.subscription)
        except (RuntimeError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Got {len(rows)} row(s). Building report...", file=sys.stderr)

    report = rows_to_report(rows, start, end, args.interval, args.model)
    tu.print_summary(report, ptu_hint=args.ptu_hint)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nWrote JSON: {args.json_path}", file=sys.stderr)
    if args.csv_path:
        tu.write_csv(report, args.csv_path, ptu_hint=args.ptu_hint)
        print(f"Wrote CSV:  {args.csv_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
