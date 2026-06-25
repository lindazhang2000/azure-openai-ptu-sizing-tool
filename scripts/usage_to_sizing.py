"""Auto-fill the PTU sizing tool's workload inputs from observed token usage.

This is a **standalone, optional** bridge between
[scripts/token_usage.py](token_usage.py) (which reads real token consumption from
Azure Monitor) and [app/ptu_core.py](../app/ptu_core.py) (the sizing logic the
Streamlit app and notebook share). It is intentionally **not** wired into the app
or the normal usage flow — run it when you want a head start on the sizing inputs
from a workload's actual traffic instead of typing them by hand.

What it does, per deployment:

  * reads the observed average and peak token throughput from a usage report,
  * derives the workload inputs the sizing tool expects (avg RPM + per-request
    input/output tokens, cache rate, P95/burst multiplier, model preset), and
  * optionally runs ``ptu_core.calculate`` so you can see the resulting PTU
    recommendation and architecture — exactly what the app would show.

Note on RPM: Azure Monitor's token metrics don't expose request counts, so the
*average tokens-per-minute* is what's grounded in real data. ``--avg-rpm`` is just
a nominal divisor used to express that throughput as "requests x per-request
tokens"; the recommended PTU and monthly volumes are **independent** of the RPM you
pick (RPM and per-request token size cancel out), so the default is fine unless you
want the printed per-request sizes to match your real request rate.

Sources (pick one):
    python scripts/usage_to_sizing.py --demo                 # built-in synthetic data
    python scripts/usage_to_sizing.py --from-json usage.json # a saved token_usage report
    python scripts/usage_to_sizing.py                        # live (needs `az login`)

Examples:
    python scripts/usage_to_sizing.py --demo --calculate
    python scripts/usage_to_sizing.py --from-json usage.json --deployment-type Global \
        --calculate --out-json sizing-inputs.json
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import sys

import token_usage as tu

DEPLOYMENT_TYPES = ["Global", "Data Zone", "Regional"]


def _period_minutes(report: dict) -> float:
    """Total minutes covered by the report's period (best effort, >0)."""
    period = report.get("period") or {}
    start = tu._parse_iso(period.get("start", ""))
    end = tu._parse_iso(period.get("end", ""))
    if start and end and end > start:
        return (end - start).total_seconds() / 60.0
    return 0.0


def usage_to_inputs(
    dep: dict,
    period_minutes: float,
    interval_minutes: float,
    *,
    core,
    avg_rpm: float = 60.0,
    cache_rate: float = 0.0,
    baseline_load_factor: float | None = None,
    safety_buffer: float | None = None,
    deployment_type: str = "Global",
    peak_minutes_fraction: float | None = None,
) -> dict:
    """Map one deployment's observed usage to a ``ptu_core.calculate`` input dict.

    Returns a dict with the sizing ``values`` plus an ``_observed`` sub-dict (the
    raw throughput figures) and ``_preset`` (the matched model preset name or None).
    """
    defaults = core.DEFAULTS
    avg_rpm = max(avg_rpm, 1e-9)
    period_minutes = max(period_minutes, 1e-9)
    interval_minutes = max(interval_minutes, 1e-9)

    totals = dep.get("totals") or {}
    prompt = float(totals.get("prompt_tokens", 0) or 0)
    generated = float(totals.get("generated_tokens", 0) or 0)
    total = float(totals.get("total_tokens", 0) or (prompt + generated))
    peak = dep.get("peak") or {}
    peak_tokens = float(peak.get("tokens", 0) or 0)

    # Observed throughput (the part grounded in real data).
    avg_input_tpm = prompt / period_minutes
    avg_output_tpm = generated / period_minutes
    avg_total_tpm = total / period_minutes
    peak_total_tpm = peak_tokens / interval_minutes
    burst = (peak_total_tpm / avg_total_tpm) if avg_total_tpm > 0 else 1.0
    burst = max(burst, 1.0)

    # Express the average throughput as RPM x per-request tokens (RPM cancels out
    # of the recommendation; it only scales the printed per-request sizes).
    avg_input_tokens = avg_input_tpm / avg_rpm
    avg_output_tokens = avg_output_tpm / avg_rpm

    preset_name, preset = core.find_model_preset(dep.get("model"))
    regional = deployment_type == "Regional"
    min_commit = preset.get(
        "regional_min_ptu_commit" if regional else "min_ptu_commit",
        defaults["min_ptu_commit"],
    )
    scale_inc = preset.get(
        "regional_ptu_scale_increment" if regional else "ptu_scale_increment",
        defaults["ptu_scale_increment"],
    )

    values = copy.deepcopy(defaults)
    values.update(
        {
            "avg_rpm": avg_rpm,
            "avg_input_tokens": avg_input_tokens,
            "avg_output_tokens": avg_output_tokens,
            "cache_rate": cache_rate,
            "p95_multiplier": burst,
            "baseline_load_factor": defaults["baseline_load_factor"]
            if baseline_load_factor is None else baseline_load_factor,
            "safety_buffer": defaults["safety_buffer"]
            if safety_buffer is None else safety_buffer,
            "peak_minutes_fraction": defaults["peak_minutes_fraction"]
            if peak_minutes_fraction is None else peak_minutes_fraction,
            "model_tpm_per_ptu": preset.get("model_tpm_per_ptu", defaults["model_tpm_per_ptu"]),
            "output_weight": preset.get("output_weight", defaults["output_weight"]),
            "min_ptu_commit": min_commit,
            "ptu_scale_increment": scale_inc,
            "spillover_supported": core.spillover_supported(deployment_type),
            "ptu_hourly_price": core.deployment_hourly_price(deployment_type),
        }
    )
    # Carry the model's confirmed PAYGO rates when available (else editable defaults).
    mult = core.paygo_multiplier(deployment_type)
    for key in ("paygo_input_per_1m", "paygo_cached_per_1m", "paygo_output_per_1m"):
        if key in preset:
            values[key] = preset[key] * mult

    values["_observed"] = {
        "avg_input_tpm": avg_input_tpm,
        "avg_output_tpm": avg_output_tpm,
        "avg_total_tpm": avg_total_tpm,
        "peak_total_tpm": peak_total_tpm,
        "peak_time": peak.get("time", ""),
        "burst_ratio": burst,
        "period_minutes": period_minutes,
        "interval_minutes": interval_minutes,
    }
    values["_preset"] = preset_name
    values["_deployment_type"] = deployment_type
    return values


def _iter_deployments(report: dict, account_filter: str | None, dep_filter: str | None):
    """Yield (account, deployment_name, deployment_dict) tuples, filtered."""
    for acc in report.get("accounts", []):
        if account_filter and account_filter.lower() not in (acc.get("name", "").lower()):
            continue
        for dep_name, dep in (acc.get("deployments") or {}).items():
            if dep_filter and dep_filter.lower() not in dep_name.lower():
                continue
            yield acc, dep_name, dep


def _fmt(n: float) -> str:
    return f"{int(round(n)):,}"


def _print_deployment(acc: dict, dep_name: str, dep: dict, values: dict, result: dict | None) -> None:
    obs = values["_observed"]
    preset = values["_preset"] or "(generic defaults)"
    model = f"{dep.get('model')}:{dep.get('version')}"
    print(f"\n{acc.get('name')} / {dep_name}  ({model})")
    print(
        f"  observed   avg {_fmt(obs['avg_total_tpm'])}/min   "
        f"peak {_fmt(obs['peak_total_tpm'])}/min "
        f"at {tu._fmt_peak_time(obs['peak_time'])}   burst {obs['burst_ratio']:.2f}x"
    )
    print(
        f"  inputs ->  avg_rpm {_fmt(values['avg_rpm'])}   "
        f"in {_fmt(values['avg_input_tokens'])} tok/req   "
        f"out {_fmt(values['avg_output_tokens'])} tok/req   "
        f"cache {values['cache_rate']:.0%}   p95 {values['p95_multiplier']:.2f}x   "
        f"model {preset}"
    )
    if result:
        arch = result.get("architecture") or {}
        print(
            f"  => {result['recommended_ptu']} PTU baseline "
            f"(peak ref {result['peak_reference_ptu']})   "
            f"{arch.get('label', '')}"
        )


def _public_inputs(values: dict) -> dict:
    """Strip the private (underscore-prefixed) helper keys for JSON export."""
    return {k: v for k, v in values.items() if not k.startswith("_")}


def _load_report(args) -> dict:
    """Get a token-usage report from --from-json, --demo, or a live query."""
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            return json.load(fh)

    now = _dt.datetime.now(_dt.timezone.utc)
    end = args.end or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = args.start or (now - _dt.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start, note = tu._enforce_retention(start, now, clamp=args.clamp)
    if note:
        print(note, file=sys.stderr)

    if args.demo:
        print("DEMO MODE: synthetic data, no Azure calls.", file=sys.stderr)
        return tu.collect_usage(
            tu._DEMO_ACCOUNTS, start, end, args.interval, None, fetch=tu._demo_fetch
        )

    print("Discovering OpenAI / AIServices accounts...", file=sys.stderr)
    accounts = tu._list_accounts(args.subscription)
    print(f"Found {len(accounts)} account(s). Querying token metrics...", file=sys.stderr)
    return tu.collect_usage(accounts, start, end, args.interval, args.subscription)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_argument_group("source (pick one)")
    src.add_argument("--from-json", help="Read a saved token_usage.py JSON report.")
    src.add_argument("--demo", action="store_true",
                     help="Use token_usage's built-in synthetic data (no Azure).")
    # Live-query options (used when neither --from-json nor a saved report is given).
    parser.add_argument("--subscription", help="Subscription ID/name for a live query.")
    parser.add_argument("--days", type=int, default=30, help="Look back N days (default 30).")
    parser.add_argument("--start", help="Start time ISO 8601 UTC (overrides --days).")
    parser.add_argument("--end", help="End time ISO 8601 UTC (default now).")
    parser.add_argument("--clamp", action="store_true",
                        help="Clamp the start to Azure's metric retention window.")
    parser.add_argument("--interval", default="PT1H",
                        help="Metric granularity for a live/demo query (default PT1H).")
    # Selection.
    parser.add_argument("--account", help="Only deployments whose account name contains this.")
    parser.add_argument("--deployment", help="Only deployments whose name contains this.")
    # Mapping assumptions.
    parser.add_argument("--avg-rpm", type=float, default=60.0,
                        help="Nominal requests/min divisor for per-request token sizes "
                             "(does not change the PTU result; default 60).")
    parser.add_argument("--cache-rate", type=float, default=0.0,
                        help="Assumed prompt cache hit rate (Monitor doesn't expose it; default 0).")
    parser.add_argument("--baseline-load-factor", type=float, default=None,
                        help="Override the baseline load factor (default from ptu_core).")
    parser.add_argument("--safety-buffer", type=float, default=None,
                        help="Override the PTU safety buffer (default from ptu_core).")
    parser.add_argument("--deployment-type", default="Global", choices=DEPLOYMENT_TYPES,
                        help="Deployment type for minimums/pricing (default Global).")
    parser.add_argument("--calculate", action="store_true",
                        help="Run ptu_core.calculate and show the PTU recommendation.")
    parser.add_argument("--out-json", help="Write the derived sizing inputs to this JSON file.")
    args = parser.parse_args(argv)

    core = tu._ptu_core()
    if not core:
        print("ERROR: could not import app/ptu_core.py (sizing logic).", file=sys.stderr)
        return 1

    try:
        report = _load_report(args)
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    period_minutes = _period_minutes(report)
    interval_minutes = report.get("interval_minutes") or tu._interval_minutes(
        (report.get("period") or {}).get("interval", "")
    )

    print(f"\nUsage -> sizing inputs   ({(report.get('period') or {}).get('start')} -> "
          f"{(report.get('period') or {}).get('end')}, deployment type {args.deployment_type})")
    print("=" * 96)

    exported: list[dict] = []
    found = False
    for acc, dep_name, dep in _iter_deployments(report, args.account, args.deployment):
        found = True
        values = usage_to_inputs(
            dep, period_minutes, interval_minutes, core=core,
            avg_rpm=args.avg_rpm, cache_rate=args.cache_rate,
            baseline_load_factor=args.baseline_load_factor,
            safety_buffer=args.safety_buffer, deployment_type=args.deployment_type,
        )
        result = core.calculate(_public_inputs(values)) if args.calculate else None
        _print_deployment(acc, dep_name, dep, values, result)
        exported.append({
            "account": acc.get("name"),
            "resource_group": acc.get("resourceGroup"),
            "location": acc.get("location"),
            "kind": acc.get("kind"),
            "deployment": dep_name,
            "model": dep.get("model"),
            "model_version": dep.get("version"),
            "observed": values["_observed"],
            "preset": values["_preset"],
            "deployment_type": values["_deployment_type"],
            "inputs": _public_inputs(values),
            "result": {
                "recommended_ptu": result["recommended_ptu"],
                "peak_reference_ptu": result["peak_reference_ptu"],
                "burst_ratio": result["burst_ratio"],
                "architecture": result["architecture"]["label"],
            } if result else None,
        })

    if not found:
        print("\n  (no matching deployments with token usage in this report)")

    print("\n" + "=" * 96)
    print("These are derived, directional inputs -- review them, then validate the result "
          "in the sizing app / official Azure PTU calculator before committing capacity.")

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as fh:
            json.dump(exported, fh, indent=2)
        print(f"\nWrote sizing inputs: {args.out_json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
