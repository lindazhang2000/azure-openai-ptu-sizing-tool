"""Shared PTU sizing logic.

Pure, dependency-free calculation used by both the Streamlit app and the
Jupyter notebook so the two cannot drift. This tool provides illustrative and
directional guidance only and is not an official Azure PTU calculator; throughput
assumptions, minimum PTU commitments, and pricing are subject to change. Always
verify against current Azure documentation before making customer-specific decisions.
"""

import csv
import datetime as _dt
import html
import io
import json
import math
import os

# All pricing constants in this module (deployment hourly $/PTU, reservation
# discounts, and per-model PAYGO $/1M-token rates) were confirmed against the
# Azure OpenAI pricing page on the date below. Bump this when you re-verify or
# update any price so drift is obvious in one place.
PRICING_CONFIRMED_AS_OF = "2026-06-25"
PRICING_SOURCE_URL = (
    "https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/"
)

DEFAULTS = {
    "avg_rpm": 60,
    "avg_input_tokens": 1800,
    "avg_output_tokens": 650,
    "p95_multiplier": 1.8,
    "peak_minutes_fraction": 0.10,
    "cache_rate": 0.20,
    "model_tpm_per_ptu": 3000,
    "output_weight": 4.0,
    "baseline_load_factor": 0.70,
    "safety_buffer": 0.15,
    "min_ptu_commit": 15,
    "ptu_scale_increment": 5,
    "ptu_hourly_price": 1.0,
    "reservation_discount_monthly": 0.64,
    "reservation_discount_yearly": 0.70,
    # Fallback PAYGO ($/1M tokens) for Custom / non-OpenAI presets. Each Azure
    # OpenAI preset below carries its own confirmed Global Standard rates; this
    # generic fallback is editable in the app/notebook. Confirm per model/region.
    "paygo_input_per_1m": 2.0,
    "paygo_cached_per_1m": 0.5,
    "paygo_output_per_1m": 8.0,
    # Priority processing is a Standard service tier billed per token at a higher
    # "priority tier" rate in exchange for a defined latency target (no PTU
    # commitment). Each supporting MODEL_PRESETS entry carries confirmed per-model
    # `priority_*_per_1m` rates; this multiplier is only the editable fallback for
    # a Custom/non-OpenAI preset that has no confirmed priority rates. The public
    # pricing page shows roughly 1.75x–2x Standard depending on model.
    # https://learn.microsoft.com/azure/foundry/openai/concepts/priority-processing
    "priority_multiplier": 1.75,
    "hours_per_month": 730,
}

# Provisioned deployment types. Global and Data Zone share the same (lower)
# minimums and scale increments; Regional uses larger model-specific minimums.
# See https://learn.microsoft.com/azure/foundry/openai/how-to/provisioned-throughput-sizing
DEPLOYMENT_TYPES = ["Global", "Data Zone", "Regional"]

# Differentiated hourly price ($/PTU/hr) by deployment type. Microsoft introduced
# differentiated hourly pricing (Dec 2024): Global is the lowest, Data Zone
# slightly higher, Regional the highest. Monthly/yearly *reservation* prices do
# NOT vary by deployment type. Values confirmed against the Azure OpenAI pricing
# page (Provisioned table, June 2026): Global $1.00, Data Zone $1.10, Regional
# $2.00 per PTU/hr; 1-month reservation $260/PTU/mo (=64% off the $730 hourly-
# equivalent), 1-year $2,652/PTU/yr (=$221/mo, ~70% off) — matching the
# reservation_discount_monthly/yearly defaults above. Re-verify before quoting.
# https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/
DEPLOYMENT_PRICING = {
    "Global": 1.0,
    "Data Zone": 1.10,
    "Regional": 2.0,
}


def deployment_hourly_price(deployment_type):
    """Return the indicative hourly $/PTU price for a deployment type."""
    return DEPLOYMENT_PRICING.get(deployment_type, DEFAULTS["ptu_hourly_price"])

# PAYGO (Standard / On-Demand) token prices also vary by deployment type. The
# per-model `paygo_*_per_1m` rates below are the **Global Standard** base; Data
# Zone Standard and Regional Standard are both exactly 10% higher than Global —
# confirmed across every Azure OpenAI model on the pricing page (June 2026), e.g.
# gpt-4.1 Global $2/$0.50/$8 -> Data Zone & Regional $2.20/$0.55/$8.80.
# https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/
PAYGO_DEPLOYMENT_MULTIPLIER = {
    "Global": 1.0,
    "Data Zone": 1.10,
    "Regional": 1.10,
}


def paygo_multiplier(deployment_type):
    """Return the Standard (PAYGO) price multiplier for a deployment type."""
    return PAYGO_DEPLOYMENT_MULTIPLIER.get(deployment_type, 1.0)

# Provisioned spillover (preview) routes overflow traffic from a provisioned
# deployment to a matching standard deployment in the same resource. It is only
# supported on Global and Data Zone provisioned deployments — NOT Regional.
# https://learn.microsoft.com/azure/foundry/openai/how-to/spillover-traffic-management
SPILLOVER_DEPLOYMENT_TYPES = ["Global", "Data Zone"]


def spillover_supported(deployment_type):
    """Return True if the deployment type supports automatic provisioned spillover."""
    return deployment_type in SPILLOVER_DEPLOYMENT_TYPES


# Priority processing (a Standard service tier billed per token at a higher
# "priority tier" rate) is supported only on Global Standard and Data Zone
# Standard (US) deployments — NOT Regional or EU Data Zone. It trades a price
# premium for a defined latency target without any provisioned commitment.
# https://learn.microsoft.com/azure/foundry/openai/concepts/priority-processing
PRIORITY_DEPLOYMENT_TYPES = ["Global", "Data Zone"]


def priority_supported(deployment_type):
    """Return True if the deployment type supports priority processing."""
    return deployment_type in PRIORITY_DEPLOYMENT_TYPES


# Per-model sizing constants from the official PTU sizing guidance
# (Input TPM per PTU, output-to-input ratio, and the deployment minimum/scale
# increment for each deployment type). `min_ptu_commit`/`ptu_scale_increment`
# are the Global & Data Zone values; `regional_*` are the Regional values.
# `available_deployments` lists the deployment types each model supports.
# `paygo_*_per_1m` are the model's confirmed **Global Standard** PAYGO rates
# ($/1M tokens) from the Azure OpenAI pricing page (June 2026) — Data Zone /
# Regional standard are exactly 10% higher (see PAYGO_DEPLOYMENT_MULTIPLIER).
# Llama-3.3-70B is priced as a Foundry MaaS model (separate pricing page), so it
# has no OpenAI PAYGO rate here and falls back to the editable DEFAULTS.
# `priority_*_per_1m` are the model's confirmed **Global** priority-processing
# rates ($/1M tokens) from the same pricing page (Data Zone priority is exactly
# 10% higher, like PAYGO). Only models that offer priority processing carry
# these keys — gpt-4.1-nano, gpt-4o, and Llama-3.3-70B do not support it and so
# omit them. Re-verify all values against current docs.
MODEL_PRESETS = {
    "gpt-5.2": {"model_tpm_per_ptu": 3400, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global"], "paygo_input_per_1m": 1.75, "paygo_cached_per_1m": 0.18, "paygo_output_per_1m": 14.0, "priority_input_per_1m": 3.50, "priority_cached_per_1m": 0.35, "priority_output_per_1m": 28.0},
    "gpt-5.1": {"model_tpm_per_ptu": 4750, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone"], "paygo_input_per_1m": 1.25, "paygo_cached_per_1m": 0.13, "paygo_output_per_1m": 10.0, "priority_input_per_1m": 2.50, "priority_cached_per_1m": 0.25, "priority_output_per_1m": 20.0},
    "gpt-5": {"model_tpm_per_ptu": 4750, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 1.25, "paygo_cached_per_1m": 0.13, "paygo_output_per_1m": 10.0, "priority_input_per_1m": 2.50, "priority_cached_per_1m": 0.25, "priority_output_per_1m": 20.0},
    "gpt-5-mini": {"model_tpm_per_ptu": 23750, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 25, "regional_ptu_scale_increment": 25, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 0.25, "paygo_cached_per_1m": 0.03, "paygo_output_per_1m": 2.0, "priority_input_per_1m": 0.45, "priority_cached_per_1m": 0.05, "priority_output_per_1m": 3.60},
    "gpt-4.1": {"model_tpm_per_ptu": 3000, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 2.0, "paygo_cached_per_1m": 0.5, "paygo_output_per_1m": 8.0, "priority_input_per_1m": 3.50, "priority_cached_per_1m": 0.88, "priority_output_per_1m": 14.0},
    "gpt-4.1-mini": {"model_tpm_per_ptu": 14900, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 25, "regional_ptu_scale_increment": 25, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 0.4, "paygo_cached_per_1m": 0.1, "paygo_output_per_1m": 1.6, "priority_input_per_1m": 0.70, "priority_cached_per_1m": 0.18, "priority_output_per_1m": 2.80},
    "gpt-4.1-nano": {"model_tpm_per_ptu": 59400, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 25, "regional_ptu_scale_increment": 25, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 0.1, "paygo_cached_per_1m": 0.03, "paygo_output_per_1m": 0.4},
    "gpt-4o": {"model_tpm_per_ptu": 2500, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 2.5, "paygo_cached_per_1m": 1.25, "paygo_output_per_1m": 10.0},
    "Llama-3.3-70B": {"model_tpm_per_ptu": 8450, "output_weight": 4.0, "min_ptu_commit": 100, "ptu_scale_increment": 100, "regional_min_ptu_commit": 100, "regional_ptu_scale_increment": 100, "available_deployments": ["Global"]},
}


def available_deployment_types(preset, model_preset_name=None):
    """Return the deployment types a model preset supports.

    When live region data (``app/region_data.json``, produced by
    ``scripts/refresh_regions.py``) is loaded and contains ``model_preset_name``,
    the deployment types are taken from that authoritative source. Otherwise the
    preset's static ``available_deployments`` list is used (a Custom/empty preset
    supports all deployment types).
    """
    if model_preset_name and _LIVE_REGION_DATA:
        live = _LIVE_REGION_DATA["models"].get(model_preset_name)
        if live:
            return [d for d in DEPLOYMENT_TYPES if d in live]
    return preset.get("available_deployments", list(DEPLOYMENT_TYPES))


def deployment_minimums(preset, deployment_type):
    """Return (min_ptu_commit, ptu_scale_increment) for a model preset and deployment type.

    Global and Data Zone share the lower minimums; Regional uses the larger
    model-specific values. Falls back to the Global/Data Zone values (or
    DEFAULTS for a Custom/empty preset) when regional values are absent.
    """
    if deployment_type == "Regional":
        return (
            preset.get("regional_min_ptu_commit", preset.get("min_ptu_commit", DEFAULTS["min_ptu_commit"])),
            preset.get("regional_ptu_scale_increment", preset.get("ptu_scale_increment", DEFAULTS["ptu_scale_increment"])),
        )
    return (
        preset.get("min_ptu_commit", DEFAULTS["min_ptu_commit"]),
        preset.get("ptu_scale_increment", DEFAULTS["ptu_scale_increment"]),
    )


def paygo_rates(preset, deployment_type):
    """Return tier-adjusted (input, cached, output) PAYGO $/1M for a preset + deployment type.

    The base rates are the model's confirmed Global Standard rates (or the
    editable DEFAULTS for a Custom/non-OpenAI preset); Data Zone and Regional
    Standard are exactly 10% higher (PAYGO_DEPLOYMENT_MULTIPLIER).
    """
    m = paygo_multiplier(deployment_type)
    base_input = preset.get("paygo_input_per_1m", DEFAULTS["paygo_input_per_1m"])
    base_cached = preset.get("paygo_cached_per_1m", DEFAULTS["paygo_cached_per_1m"])
    base_output = preset.get("paygo_output_per_1m", DEFAULTS["paygo_output_per_1m"])
    return (round(base_input * m, 4), round(base_cached * m, 4), round(base_output * m, 4))


def model_supports_priority(preset):
    """Return True if the model preset has confirmed priority-processing rates."""
    return preset.get("priority_input_per_1m") is not None and preset.get("priority_output_per_1m") is not None


def priority_rates(preset, deployment_type):
    """Return tier-adjusted (input, cached, output) priority $/1M, or ``None``.

    Returns ``None`` when the model preset has no confirmed priority rates (i.e.
    the model does not offer priority processing, e.g. gpt-4.1-nano / gpt-4o /
    Custom). The stored rates are the **Global** priority base; Data Zone
    priority is exactly 10% higher (PAYGO_DEPLOYMENT_MULTIPLIER). Priority is not
    offered on Regional deployments regardless of these rates.
    """
    if not model_supports_priority(preset):
        return None
    m = paygo_multiplier(deployment_type)
    base_input = preset["priority_input_per_1m"]
    base_cached = preset.get("priority_cached_per_1m", 0.0)
    base_output = preset["priority_output_per_1m"]
    return (round(base_input * m, 4), round(base_cached * m, 4), round(base_output * m, 4))


# Live region availability override.
#
# When present, ``app/region_data.json`` (generated by
# ``scripts/refresh_regions.py`` from the live Azure Models API) is the
# authoritative source for which provisioned deployment types a model offers and
# in which regions. The static ``_GLOBAL_REGIONS`` / ``_DATA_ZONE_REGIONS`` /
# ``_REGIONAL_REGIONS`` lists below are used only as a fallback when that file is
# absent (e.g. a fresh checkout that has not been refreshed). Loading is
# best-effort and never raises — the app must work with or without Azure creds.
_REGION_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "region_data.json")


def _load_live_region_data():
    """Load ``region_data.json`` if it exists and is well-formed, else return None."""
    try:
        with open(_REGION_DATA_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("models"), dict):
        return data
    return None


_LIVE_REGION_DATA = _load_live_region_data()


def set_live_region_data(data):
    """Override the in-memory live region data (e.g. fetched from blob storage).

    ``data`` must be the same shape as ``region_data.json`` (a dict with a
    ``models`` mapping). Pass ``None`` to revert to the static fallback. Returns
    ``True`` when the override was accepted, ``False`` when ``data`` is malformed.
    """
    global _LIVE_REGION_DATA
    if data is None:
        _LIVE_REGION_DATA = None
        return True
    if isinstance(data, dict) and isinstance(data.get("models"), dict):
        _LIVE_REGION_DATA = data
        return True
    return False


def region_data_source():
    """Describe where region availability comes from.

    Returns ``("live", generated_utc)`` when ``region_data.json`` is loaded, or
    ``("static", None)`` when falling back to the built-in indicative lists.
    """
    if _LIVE_REGION_DATA:
        return ("live", _LIVE_REGION_DATA.get("generated_utc"))
    return ("static", None)


# Indicative region availability for provisioned throughput, by deployment type.
# FALLBACK ONLY — used when region_data.json is not present (see above). Global
# routes across any region where the model is deployed; Data Zone stays within
# the US or EU data zone; Regional pins to a specific Azure region. These are
# representative subsets captured from the Microsoft Learn region tables and WILL
# drift — always confirm against the live "Region availability for Foundry Models
# sold by Azure (provisioned)" page before customer use.
# https://learn.microsoft.com/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability?pivots=provisioned

# Broad pool of regions where Global provisioned OpenAI models are commonly offered.
_GLOBAL_REGIONS = [
    "australiaeast", "brazilsouth", "canadacentral", "canadaeast", "centralus",
    "eastus", "eastus2", "francecentral", "germanywestcentral", "italynorth",
    "japaneast", "koreacentral", "northcentralus", "norwayeast", "polandcentral",
    "southcentralus", "southindia", "southeastasia", "spaincentral", "swedencentral",
    "switzerlandnorth", "uksouth", "westus", "westus3", "westeurope",
]

# Data Zone provisioned is limited to the US and EU data zones.
_DATA_ZONE_REGIONS = [
    "eastus", "eastus2", "northcentralus", "southcentralus", "westus", "westus3",
    "francecentral", "germanywestcentral", "italynorth", "polandcentral",
    "spaincentral", "swedencentral", "westeurope",
]

# Per-model Regional provisioned availability is the most constrained, so keep an
# indicative per-model list. Models that support Regional but are absent here fall
# back to a small common set.
_REGIONAL_REGIONS = {
    "gpt-5": ["australiaeast", "canadaeast", "eastus", "eastus2", "japaneast", "koreacentral", "southindia", "westus", "westus3"],
    "gpt-5-mini": ["eastus2", "koreacentral", "southindia", "westus", "westus3"],
    "gpt-4.1": ["australiaeast", "brazilsouth", "eastus", "eastus2", "japaneast", "koreacentral", "southindia", "swedencentral", "uksouth", "westus", "westus3"],
    "gpt-4.1-mini": ["australiaeast", "eastus2", "koreacentral", "southindia", "swedencentral", "westus", "westus3"],
    "gpt-4.1-nano": ["eastus", "eastus2", "swedencentral", "westus3"],
    "gpt-4o": ["australiaeast", "canadaeast", "eastus", "eastus2", "japaneast", "swedencentral", "uksouth", "westus", "westus3"],
}
_REGIONAL_FALLBACK = ["eastus", "eastus2", "westus", "westus3"]

# Indicative Global region lists for models with a narrower rollout than the pool.
_MODEL_GLOBAL_OVERRIDE = {
    "gpt-5.2": ["eastus2", "swedencentral", "westus3"],
    "Llama-3.3-70B": ["eastus2", "swedencentral", "westus3"],
}


def available_regions(model_preset_name, deployment_type):
    """Return the list of regions where a model + deployment type is offered.

    Uses live data from ``region_data.json`` when available (authoritative), and
    otherwise falls back to the built-in indicative subsets. Returns an empty
    list when the model does not support the deployment type.
    """
    if _LIVE_REGION_DATA:
        live = _LIVE_REGION_DATA["models"].get(model_preset_name)
        if live is not None:
            return list(live.get(deployment_type, []))
    preset = MODEL_PRESETS.get(model_preset_name, {})
    if deployment_type not in available_deployment_types(preset):
        return []
    if deployment_type == "Data Zone":
        return list(_DATA_ZONE_REGIONS)
    if deployment_type == "Regional":
        return list(_REGIONAL_REGIONS.get(model_preset_name, _REGIONAL_FALLBACK))
    return list(_MODEL_GLOBAL_OVERRIDE.get(model_preset_name, _GLOBAL_REGIONS))


def region_supported(model_preset_name, deployment_type, region):
    """Return True if the region appears in the indicative availability list."""
    return region in available_regions(model_preset_name, deployment_type)


def _round_up_to_increment(value, increment):
    """Round value up to the nearest valid PTU scale increment."""
    inc = max(increment, 1)
    return math.ceil(value / inc) * inc


def find_model_preset(model_name):
    """Best-effort match an Azure model name to a MODEL_PRESETS entry.

    Tries an exact (case-insensitive) match first, then progressively trims the
    trailing version component (e.g. ``gpt-4.1-2025-04-14`` -> ``gpt-4.1``).
    Returns ``(preset_name, preset_dict)`` or ``(None, {})`` when nothing matches.
    """
    if not model_name:
        return None, {}
    lowered = {k.lower(): k for k in MODEL_PRESETS}
    name = str(model_name).strip()
    candidate = name.lower()
    while candidate:
        if candidate in lowered:
            key = lowered[candidate]
            return key, MODEL_PRESETS[key]
        if "-" not in candidate:
            break
        candidate = candidate.rsplit("-", 1)[0]
    return None, {}


def suggest_ptu_for_throughput(
    weighted_tpm,
    model_tpm_per_ptu=None,
    safety_buffer=None,
    min_ptu_commit=None,
    ptu_scale_increment=None,
):
    """Suggest a baseline PTU that covers an observed weighted tokens-per-minute rate.

    Mirrors the buffer + round-up + minimum-commit logic in ``calculate`` so a
    measured peak (from ``scripts/token_usage.py``) maps to a PTU figure the same
    way the sizing tool would. ``weighted_tpm`` must already weight output tokens
    by the model's ``output_weight`` (PTU throughput is denominated in
    input-equivalent tokens). Missing parameters fall back to ``DEFAULTS``.
    """
    model_tpm_per_ptu = max(model_tpm_per_ptu or DEFAULTS["model_tpm_per_ptu"], 1)
    safety_buffer = DEFAULTS["safety_buffer"] if safety_buffer is None else safety_buffer
    min_ptu_commit = DEFAULTS["min_ptu_commit"] if min_ptu_commit is None else min_ptu_commit
    inc = max(ptu_scale_increment or DEFAULTS["ptu_scale_increment"], 1)
    raw = max(weighted_tpm, 0.0) / model_tpm_per_ptu
    buffered = raw * (1 + safety_buffer)
    return max(
        _round_up_to_increment(buffered, inc),
        _round_up_to_increment(max(min_ptu_commit, 0), inc),
    )


def calculate(values):
    ptu_scale_increment = values.get("ptu_scale_increment", DEFAULTS["ptu_scale_increment"])
    reservation_discount_monthly = values.get("reservation_discount_monthly", DEFAULTS["reservation_discount_monthly"])
    reservation_discount_yearly = values.get("reservation_discount_yearly", DEFAULTS["reservation_discount_yearly"])
    paygo_cached_per_1m = values.get("paygo_cached_per_1m", DEFAULTS["paygo_cached_per_1m"])

    avg_tpm = values["avg_rpm"] * (
        values["avg_input_tokens"] * (1 - values["cache_rate"]) +
        values["avg_output_tokens"] * values["output_weight"]
    )
    p95_tpm = avg_tpm * values["p95_multiplier"]
    baseline_tpm = p95_tpm * values["baseline_load_factor"]

    inc = max(ptu_scale_increment, 1)
    model_tpm_per_ptu = max(values["model_tpm_per_ptu"], 1)

    raw_baseline_ptu = baseline_tpm / model_tpm_per_ptu
    buffered_baseline_ptu = raw_baseline_ptu * (1 + values["safety_buffer"])
    min_commit_rounded = _round_up_to_increment(max(values["min_ptu_commit"], 0), inc)
    recommended_ptu = max(_round_up_to_increment(buffered_baseline_ptu, inc), min_commit_rounded)

    raw_peak_ptu = p95_tpm / model_tpm_per_ptu
    peak_reference_ptu = max(
        _round_up_to_increment(raw_peak_ptu * (1 + values["safety_buffer"]), inc),
        min_commit_rounded,
    )
    burst_ratio = (p95_tpm / avg_tpm) if avg_tpm > 0 else 0

    monthly_requests = values["avg_rpm"] * 60 * values["hours_per_month"]
    input_tokens_monthly = monthly_requests * values["avg_input_tokens"] * (1 - values["cache_rate"])
    cached_input_tokens_monthly = monthly_requests * values["avg_input_tokens"] * values["cache_rate"]
    output_tokens_monthly = monthly_requests * values["avg_output_tokens"]
    paygo_monthly = (
        (input_tokens_monthly / 1_000_000) * values["paygo_input_per_1m"] +
        (cached_input_tokens_monthly / 1_000_000) * paygo_cached_per_1m +
        (output_tokens_monthly / 1_000_000) * values["paygo_output_per_1m"]
    )

    # Priority processing cost lane. Priority is a Standard service tier billed
    # per token at a higher "priority tier" rate. When confirmed per-model
    # priority rates are supplied (`priority_*_per_1m`) the lane is priced from
    # them directly; otherwise it falls back to the editable multiplier over the
    # PAYGO total (for a Custom preset with no confirmed rates). Only Global and
    # Data Zone (US) Standard deployments support it; for unsupported deployment
    # types or models the figure is still computed for reference but flagged via
    # `priority_supported` so the UI can mark it not applicable.
    priority_multiplier = values.get("priority_multiplier", DEFAULTS["priority_multiplier"])
    priority_input_per_1m = values.get("priority_input_per_1m")
    priority_output_per_1m = values.get("priority_output_per_1m")
    if priority_input_per_1m is not None and priority_output_per_1m is not None:
        priority_cached_per_1m = values.get("priority_cached_per_1m")
        if priority_cached_per_1m is None:
            priority_cached_per_1m = paygo_cached_per_1m * priority_multiplier
        priority_monthly = (
            (input_tokens_monthly / 1_000_000) * priority_input_per_1m +
            (cached_input_tokens_monthly / 1_000_000) * priority_cached_per_1m +
            (output_tokens_monthly / 1_000_000) * priority_output_per_1m
        )
        priority_rate_source = "confirmed"
    else:
        priority_monthly = paygo_monthly * priority_multiplier
        priority_rate_source = "multiplier"
    priority_ok = values.get("priority_supported", True)

    ptu_hourly_monthly = recommended_ptu * values["ptu_hourly_price"] * values["hours_per_month"]
    ptu_monthly_reserved = ptu_hourly_monthly * (1 - reservation_discount_monthly)
    ptu_yearly_reserved = ptu_hourly_monthly * (1 - reservation_discount_yearly)
    # Headline PTU cost uses the 1-month reservation (typical production baseline).
    ptu_monthly = ptu_monthly_reserved

    def _per_ptu(total):
        return total / recommended_ptu if recommended_ptu else 0

    pricing_tiers = [
        {"term": "Hourly", "per_ptu_monthly": _per_ptu(ptu_hourly_monthly), "total_monthly": ptu_hourly_monthly, "savings": 0.0},
        {"term": "Monthly reservation", "per_ptu_monthly": _per_ptu(ptu_monthly_reserved), "total_monthly": ptu_monthly_reserved, "savings": reservation_discount_monthly},
        {"term": "Yearly reservation", "per_ptu_monthly": _per_ptu(ptu_yearly_reserved), "total_monthly": ptu_yearly_reserved, "savings": reservation_discount_yearly},
    ]

    # Indicative blended "PTU baseline + spillover" cost. Demand above the
    # provisioned PTU capacity spills to a Standard deployment billed at PAYGO
    # rates. A simple duty cycle models how often demand actually reaches the
    # peak: for `peak_minutes_fraction` of the time demand sits at the P95
    # level, and at the average level the rest of the time. Spill only occurs
    # (and is only paid for) when demand exceeds capacity in each regime.
    peak_minutes_fraction = values.get("peak_minutes_fraction", DEFAULTS["peak_minutes_fraction"])
    f = min(max(peak_minutes_fraction, 0.0), 1.0)
    ptu_capacity_tpm = recommended_ptu * model_tpm_per_ptu
    spill_demand = f * max(p95_tpm - ptu_capacity_tpm, 0) + (1 - f) * max(avg_tpm - ptu_capacity_tpm, 0)
    total_demand = f * p95_tpm + (1 - f) * avg_tpm
    spill_fraction = (spill_demand / total_demand) if total_demand > 0 else 0
    blended_monthly = ptu_monthly_reserved + spill_fraction * paygo_monthly

    fills_minimum = raw_baseline_ptu >= values["min_ptu_commit"]
    spillover_ok = values.get("spillover_supported", True)
    if burst_ratio >= 4:
        architecture = {
            "label": "PAYGO or smaller PTU pilot",
            "summary": "Your burstiness is high enough that PAYGO can remain the better default, or you can test a smaller PTU baseline and let Standard handle most spikes.",
            "reason": "Very high peak-to-mean ratios often push customers toward PAYGO economics unless the steady baseline is still large.",
            "badge": "🟠"
        }
    elif not fills_minimum:
        architecture = {
            "label": "PAYGO or smaller PTU pilot",
            "summary": "Your steady baseline does not yet fill the model's minimum PTU commitment, so a dedicated PTU deployment would be under-utilized. PAYGO (or a small pilot) is usually more economical here.",
            "reason": "When the baseline needs fewer PTUs than the model minimum, you pay for idle provisioned capacity.",
            "badge": "🟠"
        }
    elif burst_ratio < 2:
        architecture = {
            "label": "PTU-first production baseline",
            "summary": "Your workload looks relatively steady and large enough to fill a PTU commitment. PTU is likely a good fit for the primary production layer, then validate on hourly PTU before reservation.",
            "reason": "Lower peak-to-mean burstiness with a baseline above the model minimum aligns well with PTU economics and predictable throughput.",
            "badge": "🔵"
        }
    elif spillover_ok:
        architecture = {
            "label": "PTU + Standard spillover",
            "summary": "Recommended default for enterprise production: size PTU for the steady-state baseline and keep Standard available for bursts and overflow. This deployment type supports automatic spillover to a matching Standard deployment.",
            "reason": "Your burst profile suggests a baseline layer plus elasticity is safer than sizing PTU for every short-lived peak.",
            "badge": "🟢"
        }
    else:
        architecture = {
            "label": "PTU baseline + manual overflow (spillover unavailable)",
            "summary": "Your burst profile suits a PTU baseline plus elasticity, but this deployment type does not support automatic spillover. Either pick a Global or Data Zone provisioned deployment to enable spillover, or pair this PTU deployment with a separate Standard deployment and route overflow yourself.",
            "reason": "Automatic provisioned spillover (preview) is only supported on Global and Data Zone provisioned deployments, not Regional.",
            "badge": "🟡"
        }
    architecture["spillover_supported"] = spillover_ok

    return {
        "avg_tpm": avg_tpm,
        "p95_tpm": p95_tpm,
        "baseline_tpm": baseline_tpm,
        "raw_baseline_ptu": raw_baseline_ptu,
        "recommended_ptu": recommended_ptu,
        "peak_reference_ptu": peak_reference_ptu,
        "burst_ratio": burst_ratio,
        "spill_fraction": spill_fraction,
        "monthly_requests": monthly_requests,
        "input_tokens_monthly": input_tokens_monthly,
        "cached_input_tokens_monthly": cached_input_tokens_monthly,
        "output_tokens_monthly": output_tokens_monthly,
        "paygo_monthly": paygo_monthly,
        "priority_monthly": priority_monthly,
        "priority_multiplier": priority_multiplier,
        "priority_rate_source": priority_rate_source,
        "priority_supported": priority_ok,
        "ptu_hourly_monthly": ptu_hourly_monthly,
        "ptu_monthly_reserved": ptu_monthly_reserved,
        "ptu_yearly_reserved": ptu_yearly_reserved,
        "ptu_monthly": ptu_monthly,
        "pricing_tiers": pricing_tiers,
        "blended_monthly": blended_monthly,
        "savings_delta": paygo_monthly - ptu_monthly,
        "architecture": architecture,
        "spillover_supported": spillover_ok,
        "reservation_note": "Reservation should be treated as a billing optimization after workload validation, not as the first step and not as capacity by itself."
    }


def validate_inputs(values, foundry_mode=False):
    """Flag boundary-condition problems with the workload inputs.

    Returns a list of ``{"level": "warning"|"info", "message": str}`` so the UI
    can surface why a result might be misleading (a zero request rate, zero token
    counts, zero pricing, an implausibly large request, or a baseline that under-
    provisions with no spillover modeled). Pure and dependency-free so it is unit
    testable; the app renders each entry with ``st.warning`` / ``st.info``.
    """
    issues = []

    def num(key):
        try:
            return float(values.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    rpm_label = "Peak RPM" if foundry_mode else "Average RPM"
    if num("avg_rpm") <= 0:
        issues.append({
            "level": "warning",
            "message": f"{rpm_label} is 0 — enter a request rate to size the workload; throughput and cost are 0 until you do.",
        })

    in_tok, out_tok = num("avg_input_tokens"), num("avg_output_tokens")
    if in_tok <= 0 and out_tok <= 0:
        issues.append({
            "level": "warning",
            "message": "Input and output tokens are both 0 — set tokens per request or every cost lane reads $0.",
        })
    elif in_tok + out_tok > 200_000:
        issues.append({
            "level": "info",
            "message": f"≈{in_tok + out_tok:,.0f} tokens/request is large — confirm it fits the model's context window.",
        })

    if num("paygo_input_per_1m") <= 0 and num("paygo_output_per_1m") <= 0:
        issues.append({
            "level": "warning",
            "message": "PAYGO input and output rates are both 0 — the PAYGO and spillover lanes will read $0.",
        })

    if num("ptu_hourly_price") <= 0:
        issues.append({
            "level": "warning",
            "message": "PTU hourly price is 0 — the PTU and reservation lanes will read $0.",
        })

    if not foundry_mode and num("baseline_load_factor") < 1.0 and num("peak_minutes_fraction") <= 0:
        issues.append({
            "level": "info",
            "message": "Baseline load factor under-provisions but peak-minutes fraction is 0 — no spillover cost is modeled, so PTU + spillover equals the PTU baseline.",
        })

    return issues


def _breakeven_verdict_text(be):
    """One-line verdict for the break-even section, shared by both report builders.

    ``be`` is the verdict dict assembled in the app: ``tier``, ``current_rpm``,
    ``diff`` (PAYGO minus PTU at current load; > 0 means PTU is cheaper).
    """
    tier = be.get("tier", "PTU")
    diff = be.get("diff", 0.0)
    rpm = be.get("current_rpm", 0.0)
    if diff > 0:
        return (
            f"Provision PTU ({tier}): ~${diff:,.0f}/mo cheaper than PAYGO at the "
            f"current load of {rpm:,.0f} RPM."
        )
    return (
        f"PTU ({tier}) costs ~${-diff:,.0f}/mo more than PAYGO at {rpm:,.0f} RPM, "
        f"but may still be the right call for guaranteed throughput, steady latency, "
        f"and no 429 throttling."
    )


def build_report_html(values, calc, meta=None):
    """Render a self-contained, printable HTML sizing report for stakeholders.

    Returns a single HTML string (inline CSS, no external assets) summarising the
    workload inputs, PTU recommendation, architecture call, the four monthly cost
    lanes, and the pricing assumptions — ready to download and share, or to open
    and "Save as PDF" from the browser. ``meta`` carries the run context
    (``model``, ``deployment_type``, ``region``, ``foundry_mode``, ``title``);
    all keys are optional.
    """
    meta = meta or {}

    def esc(text):
        return html.escape(str(text))

    def money(amount):
        return f"${amount:,.0f}"

    def num(value, decimals=0):
        return f"{value:,.{decimals}f}"

    def pct(fraction, decimals=0):
        return f"{fraction * 100:,.{decimals}f}%"

    title = meta.get("title") or "Azure OpenAI PTU Sizing Report"
    model = meta.get("model") or "Custom"
    deployment_type = meta.get("deployment_type") or "-"
    region = meta.get("region") or "-"
    foundry_mode = bool(meta.get("foundry_mode"))
    generated = meta.get("generated_utc") or _dt.datetime.now(
        _dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    arch = calc.get("architecture") or {}

    # Context chips shown under the title.
    chips = [
        ("Model", model),
        ("Deployment", deployment_type),
        ("Region", region),
        ("Sizing mode", "Foundry (peak, no buffer)" if foundry_mode else "Field baseline + spillover"),
    ]
    chip_html = "".join(
        f'<span class="chip"><b>{esc(label)}:</b> {esc(value)}</span>' for label, value in chips
    )

    # Headline recommendation cards.
    cards = [
        ("Recommended PTUs", num(calc["recommended_ptu"])),
        ("Peak reference PTUs", num(calc["peak_reference_ptu"])),
        ("Burst ratio (P95 / avg)", f'{calc["burst_ratio"]:,.2f}x'),
        ("Baseline TPM", num(calc["baseline_tpm"])),
    ]
    card_html = "".join(
        f'<div class="card"><div class="card-val">{esc(v)}</div>'
        f'<div class="card-lbl">{esc(l)}</div></div>'
        for l, v in cards
    )

    # Four monthly cost lanes (priority shows n/a when it does not apply).
    if calc.get("priority_supported"):
        prio_premium = (
            (calc["priority_monthly"] / calc["paygo_monthly"] - 1) * 100
            if calc["paygo_monthly"] else 0.0
        )
        prio_src = (
            "confirmed per-model rates"
            if calc.get("priority_rate_source") == "confirmed"
            else f'~{calc["priority_multiplier"]:.2f}x PAYGO fallback'
        )
        prio_cell = money(calc["priority_monthly"])
        prio_note = f'+{prio_premium:,.0f}% vs PAYGO &middot; {esc(prio_src)}'
    else:
        prio_cell = "n/a"
        prio_note = "Needs a supported model (gpt-5.x / gpt-4.1 family) on Global or Data Zone (US) Standard."

    cost_rows = [
        ("PTU (1-month reserved)", money(calc["ptu_monthly"]),
         f'Hourly list {money(calc["ptu_hourly_monthly"])}/mo before reservation discount.'),
        ("PAYGO (Standard)", money(calc["paygo_monthly"]), "All demand billed per token."),
        ("PTU + spillover", money(calc["blended_monthly"]),
         f'~{pct(calc["spill_fraction"], 1)} of demand spills to Standard.'),
        ("Priority processing", prio_cell, prio_note),
    ]
    cost_html = "".join(
        f"<tr><td>{esc(lane)}</td><td class='r'>{cell}</td><td class='muted'>{note}</td></tr>"
        for lane, cell, note in cost_rows
    )

    savings_label = "PTU saves vs PAYGO" if calc["savings_delta"] >= 0 else "PAYGO saves vs PTU"

    # PTU reservation pricing tiers.
    tier_html = "".join(
        f"<tr><td>{esc(t['term'])}</td><td class='r'>${t['per_ptu_monthly']:,.0f}</td>"
        f"<td class='r'>{money(t['total_monthly'])}</td>"
        f"<td class='r'>{'-' if t['savings'] == 0 else pct(t['savings'])}</td></tr>"
        for t in calc.get("pricing_tiers", [])
    )

    # Break-even decision (optional) — the chart's verdict in writing.
    be = meta.get("breakeven")
    if be:
        diff = be.get("diff", 0.0)
        cross = be.get("breakeven_rpm")
        be_rows = [
            ("PTU pricing tier", esc(be.get("tier", "-"))),
            ("Current load", f'{num(be.get("current_rpm", 0))} RPM'),
            ("PTU at current load", money(be.get("ptu_now", 0))),
            ("PAYGO at current load", money(be.get("paygo_now", 0))),
            ("PTU vs PAYGO at current load",
             f'{money(abs(diff))}/mo {"cheaper" if diff > 0 else "more expensive"}'),
            ("Crossover (RPM where PTU wins)",
             f'{num(cross)} RPM' if cross else "No crossover in charted range"),
        ]
        if cross and be.get("breakeven_cost") is not None:
            be_rows.append(("Monthly cost at crossover", money(be["breakeven_cost"])))
        be_table = "".join(
            f"<tr><td>{esc(l)}</td><td class='r'>{v}</td></tr>" for l, v in be_rows
        )
        breakeven_html = (
            '<h2>Break-even decision</h2>'
            f'<table>{be_table}</table>'
            f'<p class="muted">{esc(_breakeven_verdict_text(be))}</p>'
        )
    else:
        breakeven_html = ""

    # Workload inputs.
    input_rows = [
        ("Average RPM" if not foundry_mode else "Peak RPM", num(values.get("avg_rpm", 0))),
        ("Avg input tokens / request", num(values.get("avg_input_tokens", 0))),
        ("Avg output tokens / request", num(values.get("avg_output_tokens", 0))),
        ("Prompt cache rate", pct(values.get("cache_rate", 0))),
        ("P95 load multiplier", f'{values.get("p95_multiplier", 0):,.2f}x'),
        ("Baseline load factor", f'{values.get("baseline_load_factor", 0):,.2f}'),
        ("Safety buffer", pct(values.get("safety_buffer", 0))),
        ("Peak minutes fraction", pct(values.get("peak_minutes_fraction", 0))),
    ]
    input_html = "".join(
        f"<tr><td>{esc(l)}</td><td class='r'>{esc(v)}</td></tr>" for l, v in input_rows
    )

    # Cost & throughput assumptions.
    assume_rows = [
        ("PTU hourly price (USD)", f'${values.get("ptu_hourly_price", 0):,.2f}'),
        ("Monthly reservation discount", pct(values.get("reservation_discount_monthly", 0))),
        ("Yearly reservation discount", pct(values.get("reservation_discount_yearly", 0))),
        ("PAYGO input / 1M tokens", f'${values.get("paygo_input_per_1m", 0):,.2f}'),
        ("PAYGO cached input / 1M", f'${values.get("paygo_cached_per_1m", 0):,.2f}'),
        ("PAYGO output / 1M tokens", f'${values.get("paygo_output_per_1m", 0):,.2f}'),
        ("Priority input / 1M tokens", f'${values.get("priority_input_per_1m", 0):,.2f}'),
        ("Priority output / 1M tokens", f'${values.get("priority_output_per_1m", 0):,.2f}'),
        ("Model TPM per PTU", num(values.get("model_tpm_per_ptu", 0))),
        ("Output weighting", f'{values.get("output_weight", 0):,.1f}'),
    ]
    assume_html = "".join(
        f"<tr><td>{esc(l)}</td><td class='r'>{esc(v)}</td></tr>" for l, v in assume_rows
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  :root {{ --ink:#1b1b1f; --muted:#6b6b75; --line:#e3e3ea; --accent:#0a6ed1; --bg:#f7f8fa; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; color:var(--ink);
         margin:0; background:var(--bg); }}
  .wrap {{ max-width:880px; margin:0 auto; padding:32px 28px 56px; background:#fff; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:28px 0 8px; padding-bottom:6px; border-bottom:2px solid var(--accent);
        text-transform:uppercase; letter-spacing:.04em; color:var(--accent); }}
  .sub {{ color:var(--muted); font-size:13px; margin:0 0 14px; }}
  .chips {{ margin:10px 0 4px; }}
  .chip {{ display:inline-block; background:var(--bg); border:1px solid var(--line); border-radius:14px;
           padding:3px 11px; font-size:12px; margin:0 6px 6px 0; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:12px; margin:8px 0 4px; }}
  .card {{ flex:1 1 150px; border:1px solid var(--line); border-radius:10px; padding:14px 16px; background:#fff; }}
  .card-val {{ font-size:24px; font-weight:700; color:var(--accent); }}
  .card-lbl {{ font-size:12px; color:var(--muted); margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; margin:4px 0 6px; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ background:var(--bg); font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  td.r,th.r {{ text-align:right; white-space:nowrap; }}
  td.muted,.muted {{ color:var(--muted); font-size:12px; }}
  .arch {{ border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:8px;
           padding:12px 16px; background:var(--bg); margin:6px 0; }}
  .arch b {{ font-size:14px; }}
  .foot {{ margin-top:26px; padding-top:12px; border-top:1px solid var(--line); color:var(--muted); font-size:11px; }}
  .foot a {{ color:var(--accent); }}
  .printbtn {{ position:fixed; top:16px; right:16px; background:var(--accent); color:#fff; border:0;
              border-radius:8px; padding:10px 16px; font-size:13px; cursor:pointer; box-shadow:0 2px 8px rgba(0,0,0,.15); }}
  @media print {{
    body {{ background:#fff; }}
    .wrap {{ max-width:none; padding:0; }}
    .printbtn {{ display:none; }}
    h2 {{ break-after:avoid; }}
    table,.cards,.arch {{ break-inside:avoid; }}
  }}
</style>
</head>
<body>
<button class="printbtn" onclick="window.print()">Print / Save as PDF</button>
<div class="wrap">
  <h1>{esc(title)}</h1>
  <p class="sub">Generated {esc(generated)} &middot; illustrative, directional guidance only — not an official Azure PTU calculator.</p>
  <div class="chips">{chip_html}</div>

  <h2>Recommendation</h2>
  <div class="cards">{card_html}</div>
  <div class="arch"><b>{esc(arch.get('badge', ''))} {esc(arch.get('label', ''))}</b><br>
    {esc(arch.get('summary', ''))}<br>
    <span class="muted">{esc(arch.get('reason', ''))}</span></div>

  <h2>Monthly cost comparison</h2>
  <table>
    <tr><th>Cost lane</th><th class="r">Monthly</th><th>Notes</th></tr>
    {cost_html}
  </table>
  <p class="muted"><b>{esc(savings_label)}:</b> {money(abs(calc["savings_delta"]))} / month.
     Priority processing requires model version 2025-12-01+ and Global or Data Zone (US) Standard;
     Data Zone priority covers US data zones only.</p>

  {breakeven_html}

  <h2>PTU pricing tiers (for {num(calc["recommended_ptu"])} PTUs)</h2>
  <table>
    <tr><th>Term</th><th class="r">Per PTU / mo</th><th class="r">Total / mo</th><th class="r">Savings</th></tr>
    {tier_html}
  </table>

  <h2>Workload inputs</h2>
  <table>{input_html}</table>

  <h2>Cost &amp; throughput assumptions</h2>
  <table>{assume_html}</table>

  <p class="foot">
    Pricing confirmed against the Azure OpenAI pricing page as of {esc(PRICING_CONFIRMED_AS_OF)} —
    <a href="{esc(PRICING_SOURCE_URL)}">{esc(PRICING_SOURCE_URL)}</a>.
    Throughput assumptions, minimum PTU commitments, and pricing are subject to change.
    Always verify against current Azure documentation before making customer-specific decisions.
  </p>
</div>
</body>
</html>"""

def breakeven_series(values, points=25, rpm_max=None, ptu_tier="Monthly reservation"):
    """Sweep average/peak RPM and recompute the monthly cost lanes at each step.

    PTU is fundamentally an architecture decision: below some load PAYGO is
    cheaper, above it the reserved PTU baseline wins. This returns the data to
    draw that crossover.

    ``ptu_tier`` selects which provisioned pricing tier drives the PTU lane and
    the break-even point — one of ``"Hourly"``, ``"Monthly reservation"`` or
    ``"Yearly reservation"`` (matching the ``pricing_tiers`` terms from
    :func:`calculate`). Cheaper tiers (yearly) lower the PTU slope and can move
    the crossover into range where the monthly tier never crosses.

    Returns a dict:
      - ``rows``: list of ``{rpm, paygo_monthly, ptu_monthly, blended_monthly,
        priority_monthly}`` sampled across the RPM range.
      - ``breakeven_rpm``: the RPM at which the selected PTU tier first becomes
        cheaper than PAYGO, or ``None`` if it never does within range.
      - ``current_rpm``: the RPM from ``values`` (so the UI can mark "you are here").
      - ``priority_supported``: whether the priority lane applies.
      - ``ptu_tier``: the tier label used for the PTU lane.

    Cost is recomputed via :func:`calculate`, so the PTU lane keeps its stepwise
    rounding to the scale increment.
    """
    current_rpm = max(float(values.get("avg_rpm", 0.0)), 0.0)

    def _ptu_cost(calc):
        for tier in calc.get("pricing_tiers", []):
            if tier["term"] == ptu_tier:
                return tier["total_monthly"]
        return calc["ptu_monthly"]

    def costs(rpm):
        v = dict(values)
        v["avg_rpm"] = rpm
        c = calculate(v)
        c["ptu_monthly"] = _ptu_cost(c)
        return c

    # Find break-even (first RPM where the selected PTU tier <= PAYGO) via a fine scan.
    cap = max(current_rpm * 8.0, 1.0)
    scan_steps = 240
    breakeven_rpm = None
    for i in range(1, scan_steps + 1):
        rpm = cap * i / scan_steps
        c = costs(rpm)
        if c["paygo_monthly"] >= c["ptu_monthly"]:
            breakeven_rpm = rpm
            break

    # Display range: always show the crossover and the current operating point.
    anchor = max(current_rpm, breakeven_rpm or 0.0)
    rpm_top = float(rpm_max) if rpm_max else max(anchor * 1.6, 1.0)

    priority_supported = bool(costs(rpm_top)["priority_supported"])
    rows = []
    for i in range(1, points + 1):
        rpm = rpm_top * i / points
        c = costs(rpm)
        rows.append({
            "rpm": rpm,
            "paygo_monthly": c["paygo_monthly"],
            "ptu_monthly": c["ptu_monthly"],
            "blended_monthly": c["blended_monthly"],
            "priority_monthly": c["priority_monthly"],
        })

    return {
        "rows": rows,
        "breakeven_rpm": breakeven_rpm,
        "current_rpm": current_rpm,
        "priority_supported": priority_supported,
        "ptu_tier": ptu_tier,
    }


def sensitivity_table(values, deltas=(-0.2, -0.1, 0.1, 0.2), ptu_tier="Monthly reservation"):
    """Show how the sizing and the PTU-vs-PAYGO decision move as demand flexes.

    Estimates are only as good as their inputs, so this recomputes the headline
    numbers while scaling the two most uncertain drivers independently:

      - ``rpm``: average/peak requests per minute, and
      - ``tokens``: input *and* output tokens per request (scaled together).

    For each dimension it returns one row per delta in ``deltas`` plus a ``0.0``
    "current" row, sorted ascending. Every row carries the recommended PTUs, the
    PTU monthly cost at ``ptu_tier``, the PAYGO monthly cost, their signed
    difference (``diff`` > 0 means PTU is cheaper at that point), and the
    break-even RPM for that scenario. Pure and dependency-free for unit testing.

    Returns ``{"ptu_tier", "rpm": [rows], "tokens": [rows]}`` where each row is
    ``{"delta", "label", "recommended_ptu", "ptu_monthly", "paygo_monthly",
    "diff", "breakeven_rpm"}``.
    """
    def _tier_total(calc):
        for tier in calc.get("pricing_tiers", []):
            if tier["term"] == ptu_tier:
                return tier["total_monthly"]
        return calc["ptu_monthly"]

    def _label(delta):
        if abs(delta) < 1e-9:
            return "Current"
        return f"{delta * 100:+.0f}%"

    def _row(delta, scaled):
        calc = calculate(scaled)
        be = breakeven_series(scaled, ptu_tier=ptu_tier)
        ptu_now = _tier_total(calc)
        return {
            "delta": delta,
            "label": _label(delta),
            "recommended_ptu": calc["recommended_ptu"],
            "ptu_monthly": ptu_now,
            "paygo_monthly": calc["paygo_monthly"],
            "diff": calc["paygo_monthly"] - ptu_now,  # > 0 means PTU cheaper
            "breakeven_rpm": be["breakeven_rpm"],
        }

    ordered = sorted(set(deltas) | {0.0})
    base_rpm = float(values.get("avg_rpm", 0.0) or 0.0)
    base_in = float(values.get("avg_input_tokens", 0.0) or 0.0)
    base_out = float(values.get("avg_output_tokens", 0.0) or 0.0)

    rpm_rows = [
        _row(d, {**values, "avg_rpm": base_rpm * (1 + d)})
        for d in ordered
    ]
    token_rows = [
        _row(d, {
            **values,
            "avg_input_tokens": base_in * (1 + d),
            "avg_output_tokens": base_out * (1 + d),
        })
        for d in ordered
    ]

    return {"ptu_tier": ptu_tier, "rpm": rpm_rows, "tokens": token_rows}


def build_report_csv(values, calc, meta=None):
    """Render the sizing result as a flat ``Section,Item,Value`` CSV string.

    Captures the run context, PTU recommendation, the four monthly cost lanes,
    PTU pricing tiers, workload inputs, and pricing assumptions — ready to drop
    into Excel. ``meta`` carries optional run context (``model``,
    ``deployment_type``, ``region``, ``foundry_mode``).
    """
    meta = meta or {}
    foundry_mode = bool(meta.get("foundry_mode"))

    rows = []

    def add(section, item, value):
        rows.append((section, item, value))

    add("Context", "Model", meta.get("model") or "Custom")
    add("Context", "Deployment type", meta.get("deployment_type") or "-")
    add("Context", "Region", meta.get("region") or "-")
    add("Context", "Sizing mode",
        "Foundry (peak, no buffer)" if foundry_mode else "Field baseline + spillover")
    add("Context", "Pricing confirmed as of", PRICING_CONFIRMED_AS_OF)

    add("Recommendation", "Recommended PTUs", f'{calc["recommended_ptu"]:.0f}')
    add("Recommendation", "Peak reference PTUs", f'{calc["peak_reference_ptu"]:.0f}')
    add("Recommendation", "Burst ratio (P95/avg)", f'{calc["burst_ratio"]:.2f}')
    add("Recommendation", "Baseline TPM", f'{calc["baseline_tpm"]:.0f}')
    add("Recommendation", "P95 TPM", f'{calc["p95_tpm"]:.0f}')
    add("Recommendation", "Architecture", (calc.get("architecture") or {}).get("label", ""))

    add("Cost (monthly USD)", "PTU (1-month reserved)", f'{calc["ptu_monthly"]:.2f}')
    add("Cost (monthly USD)", "PAYGO", f'{calc["paygo_monthly"]:.2f}')
    add("Cost (monthly USD)", "PTU + spillover", f'{calc["blended_monthly"]:.2f}')
    add("Cost (monthly USD)", "Priority processing",
        f'{calc["priority_monthly"]:.2f}' if calc.get("priority_supported") else "n/a")
    add("Cost (monthly USD)", "Spillover fraction", f'{calc["spill_fraction"] * 100:.1f}%')
    add("Cost (monthly USD)",
        "PTU saves vs PAYGO" if calc["savings_delta"] >= 0 else "PAYGO saves vs PTU",
        f'{abs(calc["savings_delta"]):.2f}')

    for tier in calc.get("pricing_tiers", []):
        add("PTU pricing tiers", tier["term"], f'{tier["total_monthly"]:.2f}')

    be = meta.get("breakeven")
    if be:
        diff = be.get("diff", 0.0)
        cross = be.get("breakeven_rpm")
        add("Break-even", "PTU pricing tier", be.get("tier", "-"))
        add("Break-even", "Current load (RPM)", f'{be.get("current_rpm", 0):.0f}')
        add("Break-even", "PTU at current load", f'{be.get("ptu_now", 0):.2f}')
        add("Break-even", "PAYGO at current load", f'{be.get("paygo_now", 0):.2f}')
        add("Break-even",
            "PTU cheaper than PAYGO" if diff > 0 else "PTU costlier than PAYGO",
            f'{abs(diff):.2f}')
        add("Break-even", "Crossover RPM",
            f'{cross:.0f}' if cross else "No crossover in range")
        if cross and be.get("breakeven_cost") is not None:
            add("Break-even", "Monthly cost at crossover", f'{be["breakeven_cost"]:.2f}')
        add("Break-even", "Verdict", _breakeven_verdict_text(be))

    add("Workload input", "Average RPM" if not foundry_mode else "Peak RPM",
        f'{values.get("avg_rpm", 0):.0f}')
    add("Workload input", "Avg input tokens/request", f'{values.get("avg_input_tokens", 0):.0f}')
    add("Workload input", "Avg output tokens/request", f'{values.get("avg_output_tokens", 0):.0f}')
    add("Workload input", "Prompt cache rate", f'{values.get("cache_rate", 0) * 100:.0f}%')
    add("Workload input", "P95 load multiplier", f'{values.get("p95_multiplier", 0):.2f}')
    add("Workload input", "Baseline load factor", f'{values.get("baseline_load_factor", 0):.2f}')
    add("Workload input", "Safety buffer", f'{values.get("safety_buffer", 0) * 100:.0f}%')
    add("Workload input", "Peak minutes fraction", f'{values.get("peak_minutes_fraction", 0) * 100:.0f}%')

    add("Assumption", "PTU hourly price (USD)", f'{values.get("ptu_hourly_price", 0):.2f}')
    add("Assumption", "Monthly reservation discount", f'{values.get("reservation_discount_monthly", 0) * 100:.0f}%')
    add("Assumption", "Yearly reservation discount", f'{values.get("reservation_discount_yearly", 0) * 100:.0f}%')
    add("Assumption", "PAYGO input /1M", f'{values.get("paygo_input_per_1m", 0):.2f}')
    add("Assumption", "PAYGO cached input /1M", f'{values.get("paygo_cached_per_1m", 0):.2f}')
    add("Assumption", "PAYGO output /1M", f'{values.get("paygo_output_per_1m", 0):.2f}')
    add("Assumption", "Model TPM per PTU", f'{values.get("model_tpm_per_ptu", 0):.0f}')
    add("Assumption", "Output weighting", f'{values.get("output_weight", 0):.1f}')

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Section", "Item", "Value"])
    writer.writerows(rows)
    return buf.getvalue()