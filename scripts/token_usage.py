"""Collect Azure OpenAI / AI Services token usage for a subscription.

Walks every Cognitive Services account (kind ``OpenAI`` or ``AIServices``) in the
active (or ``--subscription``) Azure subscription and pulls token-consumption
metrics from Azure Monitor, split by **deployment** and **model**:

    * ProcessedPromptTokens     -> input / prompt tokens
    * GeneratedTokens           -> output / completion tokens
    * TokenTransaction          -> total inference tokens (input + output)

The result is aggregated as subscription -> account -> deployment -> model and
printed as a readable table. It also reports **peak demand** — the busiest time
bucket (per ``--interval``, hourly by default) for each deployment, account, and
the subscription as a whole, with the peak tokens/minute rate (useful for PTU
sizing). Optionally write the full breakdown to JSON (``--json``) and/or a flat
CSV (``--csv``) for spreadsheets / dashboards.

This is a *developer/ops* reporting tool — it needs Azure credentials
(``az login``) and at least **Monitoring Reader** (or Reader) on the accounts.
Platform metrics are retained ~93 days, so pick a start time within that window.

Usage:
    az login
    python scripts/token_usage.py                      # last 30 days, hourly peaks
    python scripts/token_usage.py --days 7             # last 7 days
    python scripts/token_usage.py --interval PT5M      # finer peak resolution
    python scripts/token_usage.py --subscription <id>  # a specific subscription
    python scripts/token_usage.py --json usage.json --csv usage.csv
    python scripts/token_usage.py --start 2026-06-01T00:00:00Z --end 2026-06-15T00:00:00Z
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# Azure Monitor metric names (namespace microsoft.cognitiveservices/accounts) and
# the friendly column names used in the report / CSV. These are the canonical
# Azure OpenAI token metrics; ``TokenTransaction`` is the total (prompt + generated)
# "Processed Inference Tokens" counter.
_TOKEN_METRICS = {
    "ProcessedPromptTokens": "prompt_tokens",
    "GeneratedTokens": "generated_tokens",
    "TokenTransaction": "total_tokens",
}

# Dimension the token metrics are split by. (ModelName is NOT a supported
# dimension for these metrics, so the model is resolved from the deployment
# config instead and joined in by deployment name.)
_DIM_DEPLOYMENT = "ModelDeploymentName"

_UNKNOWN = "(unknown)"

# The metric used for peak-demand analysis (total tokens per time bucket).
_PEAK_COLUMN = "total_tokens"


def _interval_minutes(iso: str) -> float:
    """Convert an ISO 8601 duration (e.g. PT1H, PT5M, P1D) to minutes; 0 if unknown."""
    m = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", (iso or "").strip()
    )
    if not m:
        return 0.0
    days, hours, mins, secs = (int(x) if x else 0 for x in m.groups())
    return days * 1440 + hours * 60 + mins + secs / 60


def _az(args: list[str]) -> object:
    """Run an `az` CLI command and return parsed JSON, or raise on failure."""
    cmd = ["az", *args, "-o", "json"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=(os.name == "nt"),  # az is a .cmd shim on Windows
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`{' '.join(cmd)}` failed:\n{proc.stderr.strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def _resolve_subscription(subscription: str | None) -> str:
    """Return the subscription ID to use (explicit arg, or the active az subscription)."""
    if subscription:
        # Accept either an ID or a name; normalise to the ID.
        info = _az(["account", "show", "--subscription", subscription]) or {}
        return info.get("id") or subscription
    info = _az(["account", "show"]) or {}
    sub_id = info.get("id")
    if not sub_id:
        raise RuntimeError("No active subscription. Run `az login` or pass --subscription.")
    return sub_id


def _rg_from_id(resource_id: str) -> str:
    """Parse the resource group name out of an ARM resource ID."""
    parts = (resource_id or "").split("/")
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return _UNKNOWN


def _list_accounts(subscription: str | None) -> list[dict]:
    """Return all OpenAI / AIServices Cognitive Services accounts in the subscription.

    Uses the subscription-scoped ARM REST endpoint rather than
    ``az cognitiveservices account list`` so it is unaffected by any configured
    default resource group (``az configure --defaults group=...``).
    """
    sub_id = _resolve_subscription(subscription)
    url = (
        f"https://management.azure.com/subscriptions/{sub_id}"
        "/providers/Microsoft.CognitiveServices/accounts?api-version=2023-05-01"
    )
    resp = _az(["rest", "--method", "get", "--url", url]) or {}
    wanted = {"openai", "aiservices"}
    result = []
    for acc in resp.get("value") or []:
        if (acc.get("kind") or "").lower() not in wanted:
            continue
        result.append(
            {
                "name": acc.get("name"),
                "resourceGroup": _rg_from_id(acc.get("id", "")),
                "location": acc.get("location"),
                "kind": acc.get("kind"),
                "id": acc.get("id"),
            }
        )
    return result


def _dim_value(metadatavalues: list[dict], dim_name: str) -> str:
    """Pull a dimension value from a timeseries' metadatavalues (case-insensitive)."""
    target = dim_name.lower()
    for md in metadatavalues or []:
        name = ((md.get("name") or {}).get("value") or "").lower()
        if name == target:
            return md.get("value") or _UNKNOWN
    return _UNKNOWN


def _empty_counts() -> dict[str, float]:
    return {col: 0.0 for col in _TOKEN_METRICS.values()}


def _list_deployments(account: dict, subscription: str | None) -> dict[str, dict]:
    """Return {deploymentName: {"model": name, "version": ver}} for an account."""
    args = [
        "cognitiveservices", "account", "deployment", "list",
        "-n", account["name"], "-g", account["resourceGroup"],
    ]
    if subscription:
        args += ["--subscription", subscription]
    try:
        deployments = _az(args) or []
    except (RuntimeError, json.JSONDecodeError):
        return {}
    mapping: dict[str, dict] = {}
    for dep in deployments:
        model = ((dep.get("properties") or {}).get("model")) or {}
        mapping[dep.get("name")] = {
            "model": model.get("name") or _UNKNOWN,
            "version": model.get("version") or _UNKNOWN,
        }
    return mapping


def _fetch_account_usage(
    account: dict,
    start: str,
    end: str,
    interval: str,
    subscription: str | None,
) -> tuple[dict, dict, dict]:
    """Return (account, deployments, series_by_dep) for one account.

    ``deployments`` maps deployment name -> {"model","version","totals","peak"};
    ``series_by_dep`` maps deployment name -> {timestamp: total tokens}. Callers
    merge those per-deployment series (by model, account, or subscription) to find
    the concurrent peak demand of whatever set is merged.

    Token metrics are split by deployment (the only supported model-ish dimension),
    then each deployment is joined to its model name/version from the deployment
    config. A failure on one account is logged and yields an empty breakdown so the
    rest of the subscription still reports.
    """
    model_map = _list_deployments(account, subscription)

    args = [
        "monitor", "metrics", "list",
        "--resource", account["id"],
        "--metrics", *_TOKEN_METRICS.keys(),
        "--aggregation", "Total",
        "--interval", interval,
        "--start-time", start,
        "--end-time", end,
        "--filter", f"{_DIM_DEPLOYMENT} eq '*'",
    ]
    if subscription:
        args += ["--subscription", subscription]

    try:
        resp = _az(args) or {}
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"  ! {account['name']}: {exc}", file=sys.stderr)
        return account, {}, {}

    # counts_by_dep[deployment] -> counts ; series_by_dep[deployment] -> {timestamp: total}
    # The per-deployment timestamp series lets callers merge by model / account / sub
    # to find concurrent peaks (busiest bucket across whatever set is merged).
    counts_by_dep: dict[str, dict[str, float]] = {}
    series_by_dep: dict[str, dict[str, float]] = {}
    for metric in resp.get("value") or []:
        metric_name = (metric.get("name") or {}).get("value")
        column = _TOKEN_METRICS.get(metric_name)
        if not column:
            continue
        for ts in metric.get("timeseries") or []:
            deployment = _dim_value(ts.get("metadatavalues") or [], _DIM_DEPLOYMENT)
            points = ts.get("data") or []
            total = sum(point.get("total") or 0.0 for point in points)
            if total <= 0:
                continue
            counts_by_dep.setdefault(deployment, _empty_counts())[column] += total

            # Peak demand is tracked on the total-tokens metric only.
            if column == _PEAK_COLUMN:
                series = series_by_dep.setdefault(deployment, {})
                for point in points:
                    val = point.get("total") or 0.0
                    stamp = point.get("timeStamp") or point.get("timestamp") or ""
                    if val > 0:
                        series[stamp] = series.get(stamp, 0.0) + val

    deployments: dict[str, dict] = {}
    for dep_name, counts in counts_by_dep.items():
        info = model_map.get(dep_name, {})
        peak_time, peak_val = _peak_of_series(series_by_dep.get(dep_name, {}))
        deployments[dep_name] = {
            "model": info.get("model", _UNKNOWN),
            "version": info.get("version", _UNKNOWN),
            "totals": counts,
            "peak": {"tokens": peak_val, "time": peak_time},
        }
    return account, deployments, series_by_dep


def _sum_counts(target: dict[str, float], source: dict[str, float]) -> None:
    for col, val in source.items():
        target[col] = target.get(col, 0.0) + val


def collect_usage(
    accounts: list[dict],
    start: str,
    end: str,
    interval: str,
    subscription: str | None,
    workers: int = 8,
) -> dict:
    """Query every account in parallel and assemble the full usage breakdown."""
    accounts_out: list[dict] = []
    grand_total = _empty_counts()
    global_series: dict[str, float] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_account_usage, acc, start, end, interval, subscription): acc
            for acc in accounts
        }
        done = 0
        for fut in as_completed(futures):
            account, deployments, series_by_dep = fut.result()
            done += 1
            print(
                f"  [{done}/{len(accounts)}] {account['name']}: {len(deployments)} deployment(s)"
            )

            account_total = _empty_counts()
            model_totals: dict[str, dict[str, float]] = {}
            model_series: dict[str, dict[str, float]] = {}
            account_series: dict[str, float] = {}
            dep_out: dict[str, dict] = {}
            for dep_name in sorted(deployments):
                dep = deployments[dep_name]
                counts = dep["totals"]
                peak = dep.get("peak") or {"tokens": 0.0, "time": ""}
                dep_out[dep_name] = {
                    "model": dep["model"],
                    "version": dep["version"],
                    "totals": {k: round(v) for k, v in counts.items()},
                    "peak": {"tokens": round(peak["tokens"]), "time": peak["time"]},
                }
                _sum_counts(account_total, counts)
                _sum_counts(model_totals.setdefault(dep["model"], _empty_counts()), counts)
                # Merge this deployment's timestamp series into its model and the account.
                dep_series = series_by_dep.get(dep_name, {})
                _merge_series(model_series.setdefault(dep["model"], {}), dep_series)
                _merge_series(account_series, dep_series)

            # Account peak = busiest bucket across all of its deployments; a model
            # peak merges only the deployments serving that model (concurrent demand).
            acc_peak_time, acc_peak_val = _peak_of_series(account_series)
            model_peaks: dict[str, dict] = {}
            for model, series in model_series.items():
                mp_time, mp_val = _peak_of_series(series)
                model_peaks[model] = {"tokens": round(mp_val), "time": mp_time}
            for stamp, val in account_series.items():
                global_series[stamp] = global_series.get(stamp, 0.0) + val

            accounts_out.append(
                {
                    **account,
                    "deployments": dep_out,
                    "model_totals": {
                        m: {k: round(v) for k, v in c.items()}
                        for m, c in sorted(model_totals.items())
                    },
                    "model_peaks": {m: model_peaks[m] for m in sorted(model_peaks)},
                    "totals": {k: round(v) for k, v in account_total.items()},
                    "peak": {"tokens": round(acc_peak_val), "time": acc_peak_time},
                }
            )
            _sum_counts(grand_total, account_total)

    accounts_out.sort(key=lambda a: a["name"] or "")
    sub_peak_time, sub_peak_val = _peak_of_series(global_series)
    return {
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "period": {"start": start, "end": end, "interval": interval},
        "interval_minutes": _interval_minutes(interval),
        "metrics": list(_TOKEN_METRICS.keys()),
        "accounts": accounts_out,
        "totals": {k: round(v) for k, v in grand_total.items()},
        "peak": {"tokens": round(sub_peak_val), "time": sub_peak_time},
    }


def _peak_of_series(series: dict[str, float]) -> tuple[str, float]:
    """Return (timestamp, value) of the largest bucket in a timestamp->value map."""
    if not series:
        return "", 0.0
    stamp, val = max(series.items(), key=lambda kv: kv[1])
    return stamp, val


def _merge_series(target: dict[str, float], source: dict[str, float]) -> None:
    """Add a {timestamp: value} series into target in place (concurrent totals)."""
    for stamp, val in source.items():
        target[stamp] = target.get(stamp, 0.0) + val


def _fmt(n: float) -> str:
    return f"{int(round(n)):,}"


def print_summary(report: dict) -> None:
    """Print a readable per-account / per-deployment (model) table."""
    period = report["period"]
    print(
        f"\nToken usage  {period['start']} -> {period['end']}  (interval {period['interval']})"
    )
    print("=" * 96)
    cols = list(_TOKEN_METRICS.values())
    header = f"{'deployment  (model:version)':48}" + "".join(f"{c:>16}" for c in cols)

    if not report["accounts"]:
        print("  (no OpenAI / AIServices accounts found)")
        return

    for acc in report["accounts"]:
        loc = acc.get("location", "")
        print(f"\n{acc['name']}  [{acc.get('kind')} | {acc.get('resourceGroup')} | {loc}]")
        print("-" * 96)
        print(header)
        if not acc["deployments"]:
            print("  (no token metrics in this period)")
        for dep_name, dep in acc["deployments"].items():
            label = f"{dep_name}  ({dep['model']}:{dep['version']})"
            t = dep["totals"]
            print(f"{label:48}" + "".join(f"{_fmt(t.get(c, 0)):>16}" for c in cols))
        at = acc["totals"]
        print(f"{'=> account total':48}" + "".join(f"{_fmt(at.get(c, 0)):>16}" for c in cols))

    gt = report["totals"]
    print("\n" + "=" * 96)
    print(f"{'SUBSCRIPTION TOTAL':48}" + "".join(f"{_fmt(gt.get(c, 0)):>16}" for c in cols))

    _print_peak_section(report)


def _fmt_peak_time(stamp: str) -> str:
    """Trim an ISO timestamp to 'YYYY-MM-DD HH:MM' for display."""
    if not stamp:
        return "(n/a)"
    return stamp.replace("T", " ")[:16]


def _print_peak_section(report: dict) -> None:
    """Print a focused peak-demand section (busiest bucket per the interval)."""
    period = report["period"]
    minutes = report.get("interval_minutes") or _interval_minutes(period["interval"])

    def rate(tokens: float) -> str:
        return f"~{_fmt(tokens / minutes)}/min" if minutes else "n/a"

    print("\n" + "=" * 96)
    print(f"Peak demand  (busiest single {period['interval']} bucket; total tokens)")
    print("-" * 96)
    if not report["accounts"]:
        print("  (no accounts)")
        return

    for acc in report["accounts"]:
        print(f"\n{acc['name']}")
        for dep_name, dep in acc["deployments"].items():
            peak = dep.get("peak") or {}
            tokens = peak.get("tokens", 0)
            label = f"  {dep_name}  ({dep['model']}:{dep['version']})"
            print(
                f"{label:50}{_fmt(tokens):>14}  {rate(tokens):>14}  at {_fmt_peak_time(peak.get('time', ''))}"
            )
        # Per-model concurrent peak, only when a model is spread over >1 deployment
        # (otherwise it just repeats that single deployment's peak).
        model_dep_count: dict[str, int] = {}
        for dep in acc["deployments"].values():
            model_dep_count[dep["model"]] = model_dep_count.get(dep["model"], 0) + 1
        for model, mpeak in (acc.get("model_peaks") or {}).items():
            if model_dep_count.get(model, 0) > 1:
                mt = mpeak.get("tokens", 0)
                print(
                    f"{'  ~ model peak: ' + model:50}{_fmt(mt):>14}"
                    f"  {rate(mt):>14}  at {_fmt_peak_time(mpeak.get('time', ''))}"
                )
        ap = acc.get("peak") or {}
        print(
            f"{'  => account peak (all deployments)':50}{_fmt(ap.get('tokens', 0)):>14}"
            f"  {rate(ap.get('tokens', 0)):>14}  at {_fmt_peak_time(ap.get('time', ''))}"
        )

    sp = report.get("peak") or {}
    print("\n" + "-" * 96)
    print(
        f"{'SUBSCRIPTION PEAK':50}{_fmt(sp.get('tokens', 0)):>14}"
        f"  {rate(sp.get('tokens', 0)):>14}  at {_fmt_peak_time(sp.get('time', ''))}"
    )


def write_csv(report: dict, path: str) -> None:
    """Write a flat, one-row-per-(account, deployment) CSV with the model joined in."""
    cols = list(_TOKEN_METRICS.values())
    minutes = report.get("interval_minutes") or _interval_minutes(report["period"]["interval"])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["account", "resource_group", "location", "kind",
             "deployment", "model", "model_version", *cols,
             "peak_total_tokens", "peak_tokens_per_min", "peak_time"]
        )
        for acc in report["accounts"]:
            for dep_name, dep in acc["deployments"].items():
                t = dep["totals"]
                peak = dep.get("peak") or {}
                peak_tokens = int(round(peak.get("tokens", 0)))
                peak_per_min = round(peak_tokens / minutes, 1) if minutes else ""
                writer.writerow(
                    [
                        acc["name"],
                        acc.get("resourceGroup"),
                        acc.get("location"),
                        acc.get("kind"),
                        dep_name,
                        dep.get("model"),
                        dep.get("version"),
                        *(int(round(t.get(c, 0))) for c in cols),
                        peak_tokens,
                        peak_per_min,
                        peak.get("time", ""),
                    ]
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subscription", help="Subscription ID/name (default: active az subscription).",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Look back this many days from now (default 30). Ignored if --start is set.",
    )
    parser.add_argument("--start", help="Start time, ISO 8601 UTC (e.g. 2026-06-01T00:00:00Z).")
    parser.add_argument("--end", help="End time, ISO 8601 UTC (default: now).")
    parser.add_argument(
        "--interval", default="PT1H",
        help="Metric granularity (ISO 8601 duration, default PT1H). Totals are summed "
             "across buckets; peak demand is the busiest single bucket of this size.",
    )
    parser.add_argument("--json", dest="json_path", help="Write the full breakdown to this JSON file.")
    parser.add_argument("--csv", dest="csv_path", help="Write a flat per-model CSV to this file.")
    parser.add_argument("--workers", type=int, default=8, help="Parallel account queries (default 8).")
    args = parser.parse_args(argv)

    now = _dt.datetime.now(_dt.timezone.utc)
    end = args.end or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.start:
        start = args.start
    else:
        start = (now - _dt.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Discovering OpenAI / AIServices accounts...", file=sys.stderr)
    try:
        accounts = _list_accounts(args.subscription)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Found {len(accounts)} account(s). Querying token metrics...", file=sys.stderr)

    report = collect_usage(
        accounts, start, end, args.interval, args.subscription, workers=args.workers
    )

    print_summary(report)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nWrote JSON: {args.json_path}", file=sys.stderr)
    if args.csv_path:
        write_csv(report, args.csv_path)
        print(f"Wrote CSV:  {args.csv_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
