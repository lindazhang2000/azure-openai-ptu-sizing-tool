"""Shared PTU sizing logic.

Pure, dependency-free calculation used by both the Streamlit app and the
Jupyter notebook so the two cannot drift. This is an indicative workshop/demo
artifact, not the official PTU calculator. Replace model throughput, minimum
commit, and pricing assumptions with validated values before customer use.
"""

import math

DEFAULTS = {
    "avg_rpm": 60,
    "avg_input_tokens": 1800,
    "avg_output_tokens": 650,
    "p95_multiplier": 1.8,
    "cache_rate": 0.20,
    "model_tpm_per_ptu": 3000,
    "output_weight": 4.0,
    "baseline_load_factor": 0.70,
    "safety_buffer": 0.15,
    "min_ptu_commit": 15,
    "ptu_scale_increment": 5,
    "ptu_hourly_price": 15.0,
    "reservation_discount": 0.0,
    "paygo_input_per_1m": 5.0,
    "paygo_cached_per_1m": 2.5,
    "paygo_output_per_1m": 15.0,
    "hours_per_month": 730,
}

# Per-model sizing constants from the official PTU sizing guidance
# (Input TPM per PTU, output-to-input ratio, model minimum, and scale increment).
# Values are indicative defaults — confirm against current Microsoft Learn tables.
MODEL_PRESETS = {
    "gpt-4.1": {"model_tpm_per_ptu": 3000, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5},
    "gpt-5": {"model_tpm_per_ptu": 4750, "output_weight": 8.0, "min_ptu_commit": 15, "ptu_scale_increment": 5},
    "gpt-4o": {"model_tpm_per_ptu": 2500, "output_weight": 4.0, "min_ptu_commit": 15, "ptu_scale_increment": 5},
    "Llama-3.3-70B": {"model_tpm_per_ptu": 8450, "output_weight": 4.0, "min_ptu_commit": 100, "ptu_scale_increment": 100},
}


def _round_up_to_increment(value, increment):
    """Round value up to the nearest valid PTU scale increment."""
    inc = max(increment, 1)
    return math.ceil(value / inc) * inc


def calculate(values):
    ptu_scale_increment = values.get("ptu_scale_increment", DEFAULTS["ptu_scale_increment"])
    reservation_discount = values.get("reservation_discount", DEFAULTS["reservation_discount"])
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

    reserved_hourly_price = values["ptu_hourly_price"] * (1 - reservation_discount)
    ptu_hourly_monthly = recommended_ptu * values["ptu_hourly_price"] * values["hours_per_month"]
    ptu_reserved_monthly = recommended_ptu * reserved_hourly_price * values["hours_per_month"]
    ptu_monthly = ptu_reserved_monthly

    # Indicative blended "PTU baseline + spillover" cost: the recommended PTU
    # serves its capacity; demand above that (toward the P95 peak) spills to a
    # Standard deployment billed at PAYGO rates.
    ptu_capacity_tpm = recommended_ptu * model_tpm_per_ptu
    spill_tpm = max(p95_tpm - ptu_capacity_tpm, 0)
    spill_fraction = (spill_tpm / p95_tpm) if p95_tpm > 0 else 0
    blended_monthly = ptu_reserved_monthly + spill_fraction * paygo_monthly

    fills_minimum = raw_baseline_ptu >= values["min_ptu_commit"]
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
    else:
        architecture = {
            "label": "PTU + Standard spillover",
            "summary": "Recommended default for enterprise production: size PTU for the steady-state baseline and keep Standard available for bursts and overflow.",
            "reason": "Your burst profile suggests a baseline layer plus elasticity is safer than sizing PTU for every short-lived peak.",
            "badge": "🟢"
        }

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
        "ptu_reserved_monthly": ptu_reserved_monthly,
        "ptu_monthly": ptu_monthly,
        "blended_monthly": blended_monthly,
        "savings_delta": paygo_monthly - ptu_monthly,
        "architecture": architecture,
        "reservation_note": "Reservation should be treated as a billing optimization after workload validation, not as the first step and not as capacity by itself."
    }
