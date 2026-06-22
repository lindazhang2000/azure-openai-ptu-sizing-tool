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
    "ptu_hourly_price": 15.0,
    "paygo_input_per_1m": 5.0,
    "paygo_output_per_1m": 15.0,
    "hours_per_month": 730,
}


def calculate(values):
    avg_tpm = values["avg_rpm"] * (
        values["avg_input_tokens"] * (1 - values["cache_rate"]) +
        values["avg_output_tokens"] * values["output_weight"]
    )
    p95_tpm = avg_tpm * values["p95_multiplier"]
    baseline_tpm = p95_tpm * values["baseline_load_factor"]
    raw_baseline_ptu = baseline_tpm / max(values["model_tpm_per_ptu"], 1)
    recommended_ptu = max(
        math.ceil(raw_baseline_ptu * (1 + values["safety_buffer"])),
        max(math.ceil(values["min_ptu_commit"]), 0)
    )
    peak_reference_ptu = math.ceil((p95_tpm / max(values["model_tpm_per_ptu"], 1)) * (1 + values["safety_buffer"]))
    burst_ratio = (p95_tpm / baseline_tpm) if baseline_tpm > 0 else 0

    monthly_requests = values["avg_rpm"] * 60 * values["hours_per_month"]
    input_tokens_monthly = monthly_requests * values["avg_input_tokens"] * (1 - values["cache_rate"])
    output_tokens_monthly = monthly_requests * values["avg_output_tokens"]
    paygo_monthly = (
        (input_tokens_monthly / 1_000_000) * values["paygo_input_per_1m"] +
        (output_tokens_monthly / 1_000_000) * values["paygo_output_per_1m"]
    )
    ptu_monthly = recommended_ptu * values["ptu_hourly_price"] * values["hours_per_month"]

    architecture = {
        "label": "PTU + Standard spillover",
        "summary": "Recommended default for enterprise production: size PTU for the steady-state baseline and keep Standard available for bursts and overflow.",
        "reason": "Your burst profile suggests a baseline layer plus elasticity is safer than sizing PTU for every short-lived peak.",
        "badge": "🟢"
    }
    if burst_ratio < 2:
        architecture = {
            "label": "PTU-first production baseline",
            "summary": "Your workload looks relatively steady. PTU is likely a good fit for the primary production layer, then validate on hourly PTU before reservation.",
            "reason": "Lower peak-to-mean burstiness usually aligns better with PTU economics and more predictable throughput.",
            "badge": "🔵"
        }
    elif burst_ratio >= 4:
        architecture = {
            "label": "PAYGO or smaller PTU pilot",
            "summary": "Your burstiness is high enough that PAYGO can remain the better default, or you can test a smaller PTU baseline and let Standard handle most spikes.",
            "reason": "Very high peak-to-mean ratios often push customers toward PAYGO economics unless the steady baseline is still large.",
            "badge": "🟠"
        }

    return {
        "avg_tpm": avg_tpm,
        "p95_tpm": p95_tpm,
        "baseline_tpm": baseline_tpm,
        "raw_baseline_ptu": raw_baseline_ptu,
        "recommended_ptu": recommended_ptu,
        "peak_reference_ptu": peak_reference_ptu,
        "burst_ratio": burst_ratio,
        "monthly_requests": monthly_requests,
        "input_tokens_monthly": input_tokens_monthly,
        "output_tokens_monthly": output_tokens_monthly,
        "paygo_monthly": paygo_monthly,
        "ptu_monthly": ptu_monthly,
        "savings_delta": paygo_monthly - ptu_monthly,
        "architecture": architecture,
        "reservation_note": "Reservation should be treated as a billing optimization after workload validation, not as the first step and not as capacity by itself."
    }
