"""Refresh the per-model pricing overlay (``app/pricing_data.json``) from the
**Azure Retail Prices API** — a structured JSON API, not screen-scraping.

    https://prices.azure.com/api/retail/prices

Why the API instead of the pricing web page: the pricing page renders numbers in
JavaScript, so a plain fetch can't read them. The Retail Prices API returns
per-meter unit prices directly. The catch is that Azure OpenAI token meters have
terse, inconsistent names (``gpt 4.1 Inp glbl``, ``o4-mini 0416 cached Inp glbl``,
``gpt-4o-mini-0718-Outp-glbl`` …), so this script carries a small per-model
**meter spec** table (``_METER_SPECS``) to map meters to MODEL_PRESETS keys.

Scope and safety (this feeds a cost tool, so it's deliberately conservative):

* Only **Global Standard** token meters are read; the app derives Data Zone /
  Regional from the Global base, and hourly/reservation prices are separate.
* Fine-tuning, batch, provisioned, audio/realtime, and "pro"/"mini" collision
  meters are excluded.
* Only models in ``_METER_SPECS`` are touched. Newer models not yet in the
  retail feed (e.g. the gpt-5.x family, DeepSeek) keep their reviewed values.
* Only numeric, non-negative values are written; anything that fails to parse
  leaves the existing (reviewed) number in place. If nothing parses, the file is
  left unchanged.
* The script never edits code. The companion workflow runs it weekly and opens a
  **pull request** for a human to verify — it does not push to ``main``.

Usage:
    python scripts/refresh_pricing.py            # update app/pricing_data.json
    python scripts/refresh_pricing.py --dry-run  # print parsed prices, don't write
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUTPUT_PATH = os.path.join(_REPO_ROOT, "app", "pricing_data.json")

_RETAIL_URL = "https://prices.azure.com/api/retail/prices"
_SERVICE_NAME = "Foundry Models"
_SOURCE = "Azure Retail Prices API (prices.azure.com) — serviceName 'Foundry Models'"

# Meters are priced per 1,000 tokens; the app stores $/1,000,000 tokens.
_PER_1K_TO_PER_1M = 1000.0

# Meters we never want: fine-tuning, batch, provisioned, audio/realtime, tools,
# and unrelated model families that share a product.
_GLOBAL_EXCLUDE = (
    "ft", "trng", "train", "hstng", "hosting", "batch", "grdr", "dev", "prov",
    "ptu", "reserv", "commit", "fine", "transcribe", "tts", "aud", "realtime",
    "rt ", "image", "deep research", "instruct", "turbo", "vision", "computer",
    "codex", "chat", "pro",
)

# Per-model meter spec -> MODEL_PRESETS key. ``match`` tokens must all appear in
# the normalized meter name (hyphens -> spaces, lowercased); ``exclude`` tokens
# disambiguate sibling models (mini/nano/version). ``product`` is the Retail API
# productName the meter lives under. Add a row here as a model's Global Standard
# meters appear in the retail feed. Global rate only (region token "glbl").
_METER_SPECS = {
    "gpt-4.1":      {"product": "Azure OpenAI",           "match": ["gpt 4.1"],          "exclude": ["mini", "nano"]},
    "gpt-4.1-mini": {"product": "Azure OpenAI",           "match": ["gpt 4.1 mini"],     "exclude": []},
    "gpt-4.1-nano": {"product": "Azure OpenAI",           "match": ["gpt 4.1 nano"],     "exclude": []},
    "gpt-4o":       {"product": "Azure OpenAI",           "match": ["gpt 4o 1120"],      "exclude": []},
    "gpt-4o-mini":  {"product": "Azure OpenAI",           "match": ["gpt 4o mini 0718"], "exclude": []},
    "o1":           {"product": "Azure OpenAI",           "match": ["o1 1217"],          "exclude": []},
    "o3":           {"product": "Azure OpenAI",           "match": ["o3 0416"],          "exclude": []},
    "o3-mini":      {"product": "Azure OpenAI",           "match": ["o3 mini 0131"],     "exclude": []},
    "o4-mini":      {"product": "Azure OpenAI Reasoning", "match": ["o4 mini 0416"],     "exclude": []},
}

# Not yet auto-refreshable (no Global Standard token meter found in the retail
# feed as of this writing) — these keep their reviewed values in
# app/pricing_data.json. Add a _METER_SPECS row for each once its meter appears
# (confirm the product/meter wording with a quick prices.azure.com query first):
#   gpt-5, gpt-5-mini, gpt-5.1, gpt-5.1-codex, gpt-5.2, gpt-5.2-codex,
#   gpt-5.3-codex, gpt-5.4, gpt-5.4-mini, gpt-5.5,
#   DeepSeek-R1, DeepSeek-R1-0528, DeepSeek-V3-0324, DeepSeek-V3.2
# Priority-processing rates are also left to the reviewed overlay (the retail
# priority meters are not reliably distinguishable from batch by name).

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Lowercase, turn hyphens into spaces, and collapse whitespace."""
    return _WS_RE.sub(" ", text.lower().replace("-", " ")).strip()


def _valid_price(value) -> bool:
    """True if value is a real, non-negative number safe to use as a price."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "ptu-pricing-refresh"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (trusted Azure URL)
        return json.load(resp)


def fetch_product_meters(product: str) -> list[tuple[str, float]]:
    """Return ``[(normalized_meter_name, retail_price_per_1k), ...]`` for a product."""
    flt = f"serviceName eq '{_SERVICE_NAME}' and productName eq '{product}'"
    url = f"{_RETAIL_URL}?$filter={urllib.parse.quote(flt)}"
    out: list[tuple[str, float]] = []
    while url:
        data = _get(url)
        for it in data.get("Items", []):
            out.append((_norm(it["meterName"]), it["retailPrice"]))
        url = data.get("NextPageLink")
    return out


def _classify(meter: str):
    """Return 'cached' | 'input' | 'output' for a (Global, standard) token meter."""
    has_inp = "inp" in meter or "input" in meter
    if "cach" in meter and has_inp:
        return "cached"
    if "outp" in meter or "output" in meter:
        return "output"
    if has_inp:
        return "input"
    return None


def parse_from_api(meters_by_product):
    """Map retail meters onto MODEL_PRESETS pricing using ``_METER_SPECS``."""
    parsed = {}
    for preset, spec in _METER_SPECS.items():
        meters = meters_by_product.get(spec["product"], [])
        match = [_norm(t) for t in spec["match"]]
        exclude = [_norm(t) for t in spec.get("exclude", [])]
        found = {}
        for meter, price in meters:
            if "glbl" not in meter and "global" not in meter:
                continue
            if any(x in meter for x in _GLOBAL_EXCLUDE):
                continue
            if not all(tok in meter for tok in match):
                continue
            if any(tok in meter for tok in exclude):
                continue
            kind = _classify(meter)
            if kind and _valid_price(price):
                found[kind] = round(price * _PER_1K_TO_PER_1M, 6)
        if "input" in found and "output" in found:
            prices = {
                "paygo_input_per_1m": found["input"],
                "paygo_output_per_1m": found["output"],
            }
            if "cached" in found:
                prices["paygo_cached_per_1m"] = found["cached"]
            parsed[preset] = prices
    return parsed


def _load_existing() -> dict:
    try:
        with open(_OUTPUT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data.get("models"), dict):
        data["models"] = {}
    return data


def merge_overlay(existing: dict, parsed: dict) -> dict:
    """Merge validated PAYGO prices onto the existing overlay (priority/others kept)."""
    models = existing.setdefault("models", {})
    for name, prices in parsed.items():
        target = models.setdefault(name, {})
        for key, value in prices.items():
            if _valid_price(value):
                target[key] = value
    return existing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Refresh app/pricing_data.json from the Azure Retail Prices API.")
    parser.add_argument("--dry-run", action="store_true", help="Print parsed prices without writing.")
    args = parser.parse_args(argv)

    products = sorted({spec["product"] for spec in _METER_SPECS.values()})
    try:
        meters_by_product = {p: fetch_product_meters(p) for p in products}
    except Exception as exc:  # noqa: BLE001 - network failure must not crash CI
        print(f"! Retail Prices API fetch failed: {exc}", file=sys.stderr)
        return 1

    parsed = parse_from_api(meters_by_product)

    print(f"Parsed Global Standard pricing for {len(parsed)}/{len(_METER_SPECS)} model(s):")
    for name in sorted(parsed):
        print(f"  {name}: {parsed[name]}")

    if not parsed:
        print("No prices parsed (meter names may have changed). Leaving file unchanged.", file=sys.stderr)
        return 0

    data = _load_existing()
    merge_overlay(data, parsed)
    data["generated_utc"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    data["source"] = _SOURCE

    if args.dry_run:
        print("\n--dry-run: not writing.")
        return 0

    with open(_OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"\nWrote {_OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
