"""Refresh provisioned-throughput region availability from the live Azure Models API.

Queries `az cognitiveservices model list` across every physical Azure region and
records, per model, which provisioned deployment types (Global / Data Zone /
Regional) are offered in which regions. The result is written to
``app/region_data.json``, which :mod:`ptu_core` loads at import time to override
its built-in static fallback lists.

This is a *developer-time* refresh tool — it needs Azure credentials (`az login`)
and a subscription with access to the Cognitive Services / AI Services model
catalog. The public Streamlit app never calls Azure: it just reads the committed
JSON (and falls back to the static lists when the JSON is absent).

Usage:
    az login
    python scripts/refresh_regions.py            # all physical regions
    python scripts/refresh_regions.py -l eastus2 -l westus3   # a subset
    python scripts/refresh_regions.py --dry-run  # print summary, don't write

Run it periodically (it is safe to re-run) and commit the updated
``app/region_data.json`` so the live app ships current data.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# Azure provisioned SKU name -> the deployment-type label used throughout the app.
# Plain "Provisioned" is the legacy SKU and is intentionally ignored.
_SKU_TO_DEPLOYMENT_TYPE = {
    "GlobalProvisionedManaged": "Global",
    "DataZoneProvisionedManaged": "Data Zone",
    "ProvisionedManaged": "Regional",
}

# Azure model catalog names that differ from this tool's MODEL_PRESETS keys.
_MODEL_NAME_ALIASES = {
    "Llama-3.3-70B-Instruct": "Llama-3.3-70B",
}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUTPUT_PATH = os.path.join(_REPO_ROOT, "app", "region_data.json")


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


def _list_physical_regions() -> list[str]:
    """Return the names of all physical Azure regions for the active subscription."""
    locations = _az(["account", "list-locations"]) or []
    names = [
        loc["name"]
        for loc in locations
        if (loc.get("metadata") or {}).get("regionType") == "Physical"
    ]
    return sorted(set(names))


def _preset_name(model_name: str) -> str:
    return _MODEL_NAME_ALIASES.get(model_name, model_name)


def _fetch_region(region: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (region, [(preset_name, deployment_type), ...]) for one region.

    Empty list when the region has no provisioned model SKUs (or the call fails;
    a single bad region must not abort the whole refresh).
    """
    try:
        models = _az(["cognitiveservices", "model", "list", "-l", region]) or []
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"  ! {region}: {exc}", file=sys.stderr)
        return region, []

    found: set[tuple[str, str]] = set()
    for entry in models:
        if entry.get("kind") not in ("OpenAI", "AIServices"):
            continue
        model = entry.get("model") or {}
        name = model.get("name")
        if not name:
            continue
        preset = _preset_name(name)
        for sku in model.get("skus") or []:
            dep = _SKU_TO_DEPLOYMENT_TYPE.get(sku.get("name"))
            if dep:
                found.add((preset, dep))
    return region, sorted(found)


def build_region_data(regions: list[str], workers: int = 16) -> dict:
    """Query every region in parallel and assemble the model->type->regions map."""
    # models[preset][deployment_type] -> set(regions)
    models: dict[str, dict[str, set]] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_region, r): r for r in regions}
        done = 0
        for fut in as_completed(futures):
            region, pairs = fut.result()
            done += 1
            print(f"  [{done}/{len(regions)}] {region}: {len(pairs)} model/type pairs")
            for preset, dep in pairs:
                models.setdefault(preset, {}).setdefault(dep, set()).add(region)

    # Sort everything for stable, diff-friendly output.
    serialised = {
        preset: {dep: sorted(regs) for dep, regs in sorted(types.items())}
        for preset, types in sorted(models.items())
    }
    return serialised


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-l", "--location", action="append", dest="locations",
        help="Limit to specific region(s). Repeatable. Default: all physical regions.",
    )
    parser.add_argument(
        "--workers", type=int, default=16,
        help="Parallel az calls (default 16).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print a summary without writing app/region_data.json.",
    )
    parser.add_argument(
        "--output", default=_OUTPUT_PATH,
        help=f"Output path (default {_OUTPUT_PATH}).",
    )
    args = parser.parse_args(argv)

    regions = args.locations or _list_physical_regions()
    print(f"Querying {len(regions)} region(s) for provisioned model availability...")
    models = build_region_data(regions, workers=args.workers)

    payload = {
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "az cognitiveservices model list",
        "deployment_type_skus": _SKU_TO_DEPLOYMENT_TYPE,
        "models": models,
    }

    print(f"\nFound provisioned availability for {len(models)} model(s):")
    for preset, types in models.items():
        summary = ", ".join(f"{dep} ({len(regs)})" for dep, regs in types.items())
        print(f"  {preset}: {summary}")

    if args.dry_run:
        print("\n--dry-run: not writing output.")
        return 0

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
