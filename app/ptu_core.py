"""Shared PTU sizing logic.

Pure, dependency-free calculation used by both the Streamlit app and the
Jupyter notebook so the two cannot drift. This is an internal sizing tool, not
the official Azure PTU calculator; re-verify model throughput, minimum commit,
and pricing against current Azure docs before quoting customer-specific numbers.
"""

import math

# All pricing constants in this module (deployment hourly $/PTU, reservation
# discounts, and per-model PAYGO $/1M-token rates) were confirmed against the
# Azure OpenAI pricing page on the date below. Bump this when you re-verify or
# update any price so drift is obvious in one place.
PRICING_CONFIRMED_AS_OF = "2026-06-22"
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
# Re-verify all values against current docs.
MODEL_PRESETS = {
    "gpt-5.2": {"model_tpm_per_ptu": 3400, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global"], "paygo_input_per_1m": 1.75, "paygo_cached_per_1m": 0.18, "paygo_output_per_1m": 14.0},
    "gpt-5.1": {"model_tpm_per_ptu": 4750, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone"], "paygo_input_per_1m": 1.25, "paygo_cached_per_1m": 0.13, "paygo_output_per_1m": 10.0},
    "gpt-5": {"model_tpm_per_ptu": 4750, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 1.25, "paygo_cached_per_1m": 0.13, "paygo_output_per_1m": 10.0},
    "gpt-5-mini": {"model_tpm_per_ptu": 23750, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 25, "regional_ptu_scale_increment": 25, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 0.25, "paygo_cached_per_1m": 0.03, "paygo_output_per_1m": 2.0},
    "gpt-4.1": {"model_tpm_per_ptu": 3000, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 2.0, "paygo_cached_per_1m": 0.5, "paygo_output_per_1m": 8.0},
    "gpt-4.1-mini": {"model_tpm_per_ptu": 14900, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 25, "regional_ptu_scale_increment": 25, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 0.4, "paygo_cached_per_1m": 0.1, "paygo_output_per_1m": 1.6},
    "gpt-4.1-nano": {"model_tpm_per_ptu": 59400, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 25, "regional_ptu_scale_increment": 25, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 0.1, "paygo_cached_per_1m": 0.03, "paygo_output_per_1m": 0.4},
    "gpt-4o": {"model_tpm_per_ptu": 2500, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5, "regional_min_ptu_commit": 50, "regional_ptu_scale_increment": 50, "available_deployments": ["Global", "Data Zone", "Regional"], "paygo_input_per_1m": 2.5, "paygo_cached_per_1m": 1.25, "paygo_output_per_1m": 10.0},
    "Llama-3.3-70B": {"model_tpm_per_ptu": 8450, "output_weight": 4.0, "min_ptu_commit": 100, "ptu_scale_increment": 100, "regional_min_ptu_commit": 100, "regional_ptu_scale_increment": 100, "available_deployments": ["Global"]},
}


def available_deployment_types(preset):
    """Return the deployment types a model preset supports.

    A Custom/empty preset supports all deployment types.
    """
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


# Indicative region availability for provisioned throughput, by deployment type.
# Global routes across any region where the model is deployed; Data Zone stays
# within the US or EU data zone; Regional pins to a specific Azure region.
# These are representative subsets captured from the Microsoft Learn region
# tables and WILL drift — always confirm against the live "Region availability
# for Foundry Models sold by Azure (provisioned)" page before customer use.
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
    """Return an indicative list of regions where a model + deployment type is offered.

    Returns an empty list when the model does not support the deployment type.
    Indicative subsets only — confirm against the live Microsoft Learn region tables.
    """
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
