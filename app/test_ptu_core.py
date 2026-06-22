import math

import pytest

from ptu_core import (
    DEFAULTS,
    DEPLOYMENT_PRICING,
    DEPLOYMENT_TYPES,
    MODEL_PRESETS,
    available_deployment_types,
    calculate,
    deployment_hourly_price,
    deployment_minimums,
)


def test_defaults_match_expected_scenario():
    r = calculate(DEFAULTS)

    # Throughput proxy: 60 * (1800*0.8 + 650*4) = 60 * (1440 + 2600) = 242400
    assert r["avg_tpm"] == pytest.approx(242400.0)
    assert r["p95_tpm"] == pytest.approx(242400.0 * 1.8)              # 436320
    assert r["baseline_tpm"] == pytest.approx(242400.0 * 1.8 * 0.70)  # 305424

    # Sizing: raw = 305424/3000 = 101.808; *1.15 = 117.0792; round UP to 5 -> 120
    assert r["raw_baseline_ptu"] == pytest.approx(101.808)
    assert r["recommended_ptu"] == 120
    # peak: (436320/3000)*1.15 = 167.256; round UP to 5 -> 170
    assert r["peak_reference_ptu"] == 170

    # burst_ratio = p95/avg = p95_multiplier (peak-to-mean)
    assert r["burst_ratio"] == pytest.approx(1.8)

    # Cost: hourly list = 120 PTU * $1/hr * 730h; 1-mo reservation = 64% off
    assert r["ptu_hourly_monthly"] == pytest.approx(120 * 1.0 * 730)
    assert r["ptu_monthly_reserved"] == pytest.approx(120 * 1.0 * 730 * (1 - 0.64))
    assert r["ptu_yearly_reserved"] == pytest.approx(120 * 1.0 * 730 * (1 - 0.70))
    assert r["ptu_monthly"] == pytest.approx(r["ptu_monthly_reserved"])
    assert r["savings_delta"] == pytest.approx(r["paygo_monthly"] - r["ptu_monthly"])

    # Steady, baseline above minimum -> PTU-first
    assert r["architecture"]["label"] == "PTU-first production baseline"


def test_recommended_ptu_rounds_up_to_scale_increment():
    # increment 5 -> result is always a multiple of 5
    r = calculate(DEFAULTS)
    assert r["recommended_ptu"] % DEFAULTS["ptu_scale_increment"] == 0
    assert r["peak_reference_ptu"] % DEFAULTS["ptu_scale_increment"] == 0
    # Llama-style increment of 100 rounds to a multiple of 100
    vals = {**DEFAULTS, **MODEL_PRESETS["Llama-3.3-70B"], "avg_rpm": 1000}
    r2 = calculate(vals)
    assert r2["recommended_ptu"] % 100 == 0


def test_cached_tokens_billed_at_discounted_rate_not_free():
    no_cache_credit = {**DEFAULTS, "paygo_cached_per_1m": 0.0}
    with_cache_cost = {**DEFAULTS, "paygo_cached_per_1m": 2.5}
    cheaper = calculate(no_cache_credit)
    pricier = calculate(with_cache_cost)
    # Charging for cached tokens must increase PAYGO cost
    assert pricier["paygo_monthly"] > cheaper["paygo_monthly"]
    assert pricier["cached_input_tokens_monthly"] > 0


def test_reservation_tiers_discount_hourly_price():
    r = calculate(DEFAULTS)
    tiers = {t["term"]: t for t in r["pricing_tiers"]}
    assert tiers["Hourly"]["savings"] == 0.0
    assert tiers["Monthly reservation"]["savings"] == pytest.approx(0.64)
    assert tiers["Yearly reservation"]["savings"] == pytest.approx(0.70)
    # Reserved tiers are cheaper than hourly, yearly cheapest
    assert tiers["Monthly reservation"]["total_monthly"] < tiers["Hourly"]["total_monthly"]
    assert tiers["Yearly reservation"]["total_monthly"] < tiers["Monthly reservation"]["total_monthly"]
    # Per-PTU figure is the monthly total divided by PTU count
    assert tiers["Hourly"]["per_ptu_monthly"] == pytest.approx(1.0 * 730)


def test_gpt51_preset_matches_foundry_calculator():
    # Foundry calculator: gpt-5.1, Peak RPM 200, 2000 input / 400 output, 50% cache -> 180 PTUs.
    # Foundry sizes for peak with no buffer/baseline factor.
    vals = {
        **DEFAULTS,
        **MODEL_PRESETS["gpt-5.1"],
        "avg_rpm": 200,
        "avg_input_tokens": 2000,
        "avg_output_tokens": 400,
        "cache_rate": 0.50,
        "p95_multiplier": 1.0,
        "baseline_load_factor": 1.0,
        "safety_buffer": 0.0,
    }
    r = calculate(vals)
    assert r["recommended_ptu"] == 180


def test_blended_spillover_between_reserved_and_paygo():
    r = calculate(DEFAULTS)
    assert 0 <= r["spill_fraction"] <= 1
    # Blended = reserved baseline + a fraction of PAYGO -> never below reserved
    assert r["blended_monthly"] >= r["ptu_monthly_reserved"]
    assert r["blended_monthly"] == pytest.approx(
        r["ptu_monthly_reserved"] + r["spill_fraction"] * r["paygo_monthly"]
    )


def test_peak_minutes_fraction_scales_spillover():
    # More time spent at peak -> more demand above capacity spills -> higher blended cost
    low = calculate({**DEFAULTS, "peak_minutes_fraction": 0.05})
    high = calculate({**DEFAULTS, "peak_minutes_fraction": 0.50})
    assert high["spill_fraction"] > low["spill_fraction"]
    assert high["blended_monthly"] > low["blended_monthly"]


def test_capacity_above_peak_has_no_spillover():
    # Huge PTU baseline (factor 1.0, big buffer) so capacity covers the P95 peak
    vals = {**DEFAULTS, "baseline_load_factor": 1.0, "safety_buffer": 0.5, "peak_minutes_fraction": 0.5}
    r = calculate(vals)
    assert r["spill_fraction"] == pytest.approx(0.0)
    assert r["blended_monthly"] == pytest.approx(r["ptu_monthly_reserved"])


def test_min_ptu_commit_floor_applies_for_tiny_workload():
    vals = {**DEFAULTS, "avg_rpm": 1, "min_ptu_commit": 15}
    r = calculate(vals)
    assert r["recommended_ptu"] == 15


def test_zero_throughput_is_safe():
    vals = {**DEFAULTS, "avg_rpm": 0}
    r = calculate(vals)
    assert r["baseline_tpm"] == 0
    assert r["burst_ratio"] == 0
    # min commit still enforced
    assert r["recommended_ptu"] == DEFAULTS["min_ptu_commit"]


def test_model_tpm_per_ptu_zero_does_not_divide_by_zero():
    vals = {**DEFAULTS, "model_tpm_per_ptu": 0}
    r = calculate(vals)  # max(model_tpm_per_ptu, 1) guard
    assert math.isfinite(r["raw_baseline_ptu"])
    assert math.isfinite(r["peak_reference_ptu"])


@pytest.mark.parametrize(
    "p95,expected_label",
    [
        (1.8, "PTU-first production baseline"),   # burst_ratio = 1.8  (<2), fills minimum
        (3.0, "PTU + Standard spillover"),        # burst_ratio = 3.0  (2..4)
        (4.0, "PAYGO or smaller PTU pilot"),      # burst_ratio = 4.0  (>=4)
    ],
)
def test_architecture_recommendation_by_burstiness(p95, expected_label):
    # burst_ratio is peak-to-mean (= p95_multiplier)
    vals = {**DEFAULTS, "p95_multiplier": p95}
    r = calculate(vals)
    assert r["burst_ratio"] == pytest.approx(p95)
    assert r["architecture"]["label"] == expected_label


def test_small_baseline_below_minimum_recommends_paygo():
    # Steady (low burst) but baseline needs fewer PTUs than the model minimum
    vals = {**DEFAULTS, "avg_rpm": 1}
    r = calculate(vals)
    assert r["raw_baseline_ptu"] < DEFAULTS["min_ptu_commit"]
    assert r["architecture"]["label"] == "PAYGO or smaller PTU pilot"


def test_deployment_types_available():
    assert DEPLOYMENT_TYPES == ["Global", "Data Zone", "Regional"]


@pytest.mark.parametrize("deployment_type", ["Global", "Data Zone"])
def test_global_and_data_zone_use_lower_minimums(deployment_type):
    # Global and Data Zone share the model's lower minimum/increment (e.g. 15/5).
    preset = MODEL_PRESETS["gpt-4.1"]
    min_ptu, increment = deployment_minimums(preset, deployment_type)
    assert min_ptu == 15
    assert increment == 5


def test_regional_uses_larger_model_specific_minimums():
    # gpt-4.1 Regional provisioned minimum is 50 PTUs in increments of 50.
    min_ptu, increment = deployment_minimums(MODEL_PRESETS["gpt-4.1"], "Regional")
    assert min_ptu == 50
    assert increment == 50
    # gpt-5-mini Regional minimum is 25/25.
    mini_min, mini_inc = deployment_minimums(MODEL_PRESETS["gpt-5-mini"], "Regional")
    assert mini_min == 25
    assert mini_inc == 25


def test_custom_preset_falls_back_to_defaults():
    # An empty (Custom) preset keeps the default minimums for every type.
    for dtype in DEPLOYMENT_TYPES:
        min_ptu, increment = deployment_minimums({}, dtype)
        assert min_ptu == DEFAULTS["min_ptu_commit"]
        assert increment == DEFAULTS["ptu_scale_increment"]


def test_regional_minimum_raises_sizing_floor():
    # A tiny workload on gpt-4.1: Global floors at 15, Regional floors at 50.
    base = {**DEFAULTS, **MODEL_PRESETS["gpt-4.1"], "avg_rpm": 1}
    g_min, g_inc = deployment_minimums(MODEL_PRESETS["gpt-4.1"], "Global")
    r_min, r_inc = deployment_minimums(MODEL_PRESETS["gpt-4.1"], "Regional")
    g = calculate({**base, "min_ptu_commit": g_min, "ptu_scale_increment": g_inc})
    r = calculate({**base, "min_ptu_commit": r_min, "ptu_scale_increment": r_inc})
    assert g["recommended_ptu"] == 15
    assert r["recommended_ptu"] == 50


def test_available_deployment_types_restrict_per_model():
    # gpt-5.2 is Global only; gpt-5.1 adds Data Zone; gpt-4.1 supports all three.
    assert available_deployment_types(MODEL_PRESETS["gpt-5.2"]) == ["Global"]
    assert available_deployment_types(MODEL_PRESETS["gpt-5.1"]) == ["Global", "Data Zone"]
    assert available_deployment_types(MODEL_PRESETS["gpt-4.1"]) == ["Global", "Data Zone", "Regional"]
    # Llama is Global only.
    assert available_deployment_types(MODEL_PRESETS["Llama-3.3-70B"]) == ["Global"]


def test_available_deployment_types_default_to_all_for_custom():
    # A Custom/empty preset offers every deployment type.
    assert available_deployment_types({}) == DEPLOYMENT_TYPES


def test_every_preset_lists_at_least_one_deployment_type():
    for name, preset in MODEL_PRESETS.items():
        types = available_deployment_types(preset)
        assert len(types) >= 1, name
        assert all(t in DEPLOYMENT_TYPES for t in types), name


def test_hourly_price_is_differentiated_by_deployment_type():
    # Global is cheapest, Data Zone higher, Regional the most expensive.
    g = deployment_hourly_price("Global")
    d = deployment_hourly_price("Data Zone")
    r = deployment_hourly_price("Regional")
    assert g < d < r
    assert DEPLOYMENT_PRICING["Global"] == g
    # An unknown type falls back to the default hourly price.
    assert deployment_hourly_price("Nonexistent") == DEFAULTS["ptu_hourly_price"]


def test_regional_costs_more_than_global_for_same_workload():
    # Same PTU count, but Regional's higher hourly price -> higher cost in every tier.
    g = calculate({**DEFAULTS, "ptu_hourly_price": deployment_hourly_price("Global")})
    r = calculate({**DEFAULTS, "ptu_hourly_price": deployment_hourly_price("Regional")})
    assert r["ptu_hourly_monthly"] > g["ptu_hourly_monthly"]
    assert r["ptu_monthly_reserved"] > g["ptu_monthly_reserved"]



