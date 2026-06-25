import math

import pytest

import ptu_core
from ptu_core import (
    DEFAULTS,
    DEPLOYMENT_PRICING,
    DEPLOYMENT_TYPES,
    MODEL_PRESETS,
    PAYGO_DEPLOYMENT_MULTIPLIER,
    SPILLOVER_DEPLOYMENT_TYPES,
    available_deployment_types,
    available_regions,
    calculate,
    deployment_hourly_price,
    deployment_minimums,
    paygo_multiplier,
    paygo_rates,
    model_supports_priority,
    priority_rates,
    priority_supported,
    region_supported,
    spillover_supported,
)
from ptu_core import find_model_preset, suggest_ptu_for_throughput


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


def test_priority_lane_scales_paygo_by_multiplier():
    r = calculate(DEFAULTS)
    # Priority = PAYGO token volume billed at the priority tier multiplier.
    assert r["priority_multiplier"] == pytest.approx(DEFAULTS["priority_multiplier"])
    assert r["priority_monthly"] == pytest.approx(r["paygo_monthly"] * DEFAULTS["priority_multiplier"])
    # A priority premium (>1x) costs more than plain PAYGO.
    assert r["priority_monthly"] > r["paygo_monthly"]


def test_priority_multiplier_is_editable():
    r = calculate({**DEFAULTS, "priority_multiplier": 2.5})
    assert r["priority_monthly"] == pytest.approx(r["paygo_monthly"] * 2.5)


def test_priority_supported_only_global_and_data_zone():
    assert priority_supported("Global") is True
    assert priority_supported("Data Zone") is True
    assert priority_supported("Regional") is False
    # The flag flows through calculate() for the UI to mark it not applicable.
    assert calculate({**DEFAULTS, "priority_supported": False})["priority_supported"] is False


def test_model_supports_priority_only_for_models_with_confirmed_rates():
    assert model_supports_priority(MODEL_PRESETS["gpt-4.1"]) is True
    assert model_supports_priority(MODEL_PRESETS["gpt-5"]) is True
    # Models with no priority column on the pricing page lack the rates.
    assert model_supports_priority(MODEL_PRESETS["gpt-4.1-nano"]) is False
    assert model_supports_priority(MODEL_PRESETS["gpt-4o"]) is False
    assert model_supports_priority(MODEL_PRESETS["Llama-3.3-70B"]) is False
    # A Custom preset (empty) has no priority rates.
    assert model_supports_priority({}) is False


def test_priority_rates_none_when_model_has_no_priority():
    assert priority_rates(MODEL_PRESETS["gpt-4.1-nano"], "Global") is None
    assert priority_rates({}, "Global") is None


def test_priority_rates_data_zone_is_ten_percent_above_global():
    g_in, g_cached, g_out = priority_rates(MODEL_PRESETS["gpt-4.1"], "Global")
    d_in, d_cached, d_out = priority_rates(MODEL_PRESETS["gpt-4.1"], "Data Zone")
    assert (g_in, g_cached, g_out) == pytest.approx((3.50, 0.88, 14.0))
    assert d_in == pytest.approx(g_in * 1.10)
    assert d_cached == pytest.approx(g_cached * 1.10)
    assert d_out == pytest.approx(g_out * 1.10)


def test_priority_lane_uses_confirmed_rates_when_supplied():
    prio_in, prio_cached, prio_out = priority_rates(MODEL_PRESETS["gpt-4.1"], "Global")
    vals = {
        **DEFAULTS,
        "priority_input_per_1m": prio_in,
        "priority_cached_per_1m": prio_cached,
        "priority_output_per_1m": prio_out,
    }
    r = calculate(vals)
    assert r["priority_rate_source"] == "confirmed"
    expected = (
        (r["input_tokens_monthly"] / 1_000_000) * prio_in +
        (r["cached_input_tokens_monthly"] / 1_000_000) * prio_cached +
        (r["output_tokens_monthly"] / 1_000_000) * prio_out
    )
    assert r["priority_monthly"] == pytest.approx(expected)
    # Confirmed rates are not a flat multiple of the PAYGO total.
    assert r["priority_monthly"] != pytest.approx(r["paygo_monthly"] * DEFAULTS["priority_multiplier"])


def test_priority_lane_falls_back_to_multiplier_without_confirmed_rates():
    r = calculate(DEFAULTS)
    assert r["priority_rate_source"] == "multiplier"
    assert r["priority_monthly"] == pytest.approx(r["paygo_monthly"] * DEFAULTS["priority_multiplier"])


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


def test_spillover_supported_only_for_global_and_data_zone():
    assert spillover_supported("Global") is True
    assert spillover_supported("Data Zone") is True
    assert spillover_supported("Regional") is False
    assert SPILLOVER_DEPLOYMENT_TYPES == ["Global", "Data Zone"]


def test_spillover_supported_default_preserves_legacy_recommendation():
    # No spillover_supported key -> defaults to True -> classic spillover label.
    r = calculate({**DEFAULTS, "p95_multiplier": 3.0})
    assert r["spillover_supported"] is True
    assert r["architecture"]["label"] == "PTU + Standard spillover"
    assert r["architecture"]["spillover_supported"] is True


def test_regional_burst_recommendation_flags_no_spillover():
    # Same burst profile, but Regional cannot auto-spill -> manual overflow label.
    r = calculate({**DEFAULTS, "p95_multiplier": 3.0, "spillover_supported": False})
    assert r["spillover_supported"] is False
    assert r["architecture"]["label"] == "PTU baseline + manual overflow (spillover unavailable)"
    assert r["architecture"]["spillover_supported"] is False


def test_spillover_flag_does_not_change_non_spillover_recommendations():
    # Steady (burst < 2) recommends PTU-first regardless of spillover support.
    steady_global = calculate({**DEFAULTS, "p95_multiplier": 1.8})
    steady_regional = calculate({**DEFAULTS, "p95_multiplier": 1.8, "spillover_supported": False})
    assert steady_global["architecture"]["label"] == "PTU-first production baseline"
    assert steady_regional["architecture"]["label"] == "PTU-first production baseline"


@pytest.fixture
def static_regions(monkeypatch):
    """Force the built-in fallback region lists (no live region_data.json).

    The static-fallback region assertions below must be deterministic whether or
    not a developer has run scripts/refresh_regions.py, so disable the live
    override for these tests.
    """
    monkeypatch.setattr(ptu_core, "_LIVE_REGION_DATA", None)


def test_available_regions_empty_for_unsupported_deployment_type(static_regions):
    # In the static fallback, gpt-5.2 is Global only, so it has no Data Zone or
    # Regional regions. (Live data may differ and is covered separately.)
    assert available_regions("gpt-5.2", "Data Zone") == []
    assert available_regions("gpt-5.2", "Regional") == []
    assert available_regions("gpt-5.2", "Global")  # non-empty


def test_data_zone_regions_are_us_and_eu_only(static_regions):
    # Data Zone provisioned stays in US/EU zones — no APAC regions like australiaeast.
    regions = available_regions("gpt-4.1", "Data Zone")
    assert "eastus" in regions
    assert "westeurope" in regions
    assert "australiaeast" not in regions
    assert "japaneast" not in regions


def test_region_supported_checks_indicative_list(static_regions):
    assert region_supported("gpt-4.1", "Data Zone", "eastus") is True
    assert region_supported("gpt-4.1", "Data Zone", "australiaeast") is False
    assert region_supported("gpt-4.1", "Global", "eastus2") is True
    # Unsupported deployment type -> no regions -> always False.
    assert region_supported("gpt-5.2", "Regional", "eastus2") is False


def test_regional_regions_are_model_specific_with_fallback(static_regions):
    # Known model uses its curated list; an unmapped (Custom) model uses the fallback.
    assert "eastus2" in available_regions("gpt-4.1", "Regional")
    fallback = available_regions("Custom", "Regional")
    assert fallback == ["eastus", "eastus2", "westus", "westus3"]


def test_live_region_data_overrides_static(monkeypatch):
    # When region_data.json is loaded, it is authoritative for both the
    # available deployment types and the region lists.
    fake = {
        "generated_utc": "2026-06-23T00:00:00+00:00",
        "models": {
            "gpt-5.2": {
                "Global": ["eastus2", "swedencentral"],
                "Data Zone": ["eastus2"],
            }
        },
    }
    monkeypatch.setattr(ptu_core, "_LIVE_REGION_DATA", fake)

    # Live data says gpt-5.2 now offers Data Zone (the static fallback did not).
    assert available_regions("gpt-5.2", "Data Zone") == ["eastus2"]
    assert available_regions("gpt-5.2", "Global") == ["eastus2", "swedencentral"]
    # A deployment type absent from live data yields no regions.
    assert available_regions("gpt-5.2", "Regional") == []
    # Deployment types are driven by live data, in canonical order.
    assert available_deployment_types(MODEL_PRESETS["gpt-5.2"], "gpt-5.2") == ["Global", "Data Zone"]
    # region_data_source reports the live provenance.
    assert ptu_core.region_data_source() == ("live", "2026-06-23T00:00:00+00:00")


def test_region_data_source_static_without_live_data(static_regions):
    assert ptu_core.region_data_source() == ("static", None)


def test_openai_presets_carry_confirmed_global_paygo_rates():
    # Confirmed Global Standard $/1M rates from the Azure OpenAI pricing page.
    assert MODEL_PRESETS["gpt-4.1"]["paygo_input_per_1m"] == 2.0
    assert MODEL_PRESETS["gpt-4.1"]["paygo_output_per_1m"] == 8.0
    assert MODEL_PRESETS["gpt-4o"]["paygo_output_per_1m"] == 10.0
    assert MODEL_PRESETS["gpt-5.1"]["paygo_output_per_1m"] == 10.0


def test_per_model_paygo_changes_paygo_monthly():
    # A cheaper model (gpt-4.1-nano) must yield a lower PAYGO cost than gpt-4o
    # for the same workload, because per-model token rates flow into calculate().
    workload = {"avg_rpm": 100, "avg_input_tokens": 1500, "avg_output_tokens": 500, "p95_multiplier": 1.5, "cache_rate": 0.2}
    nano = calculate({**DEFAULTS, **MODEL_PRESETS["gpt-4.1-nano"], **workload})
    gpt4o = calculate({**DEFAULTS, **MODEL_PRESETS["gpt-4o"], **workload})
    assert nano["paygo_monthly"] < gpt4o["paygo_monthly"]


def test_llama_preset_has_no_openai_paygo_override():
    # Llama-3.3-70B is a Foundry MaaS model; it carries no OpenAI PAYGO rate and
    # falls back to the editable DEFAULTS.
    assert "paygo_input_per_1m" not in MODEL_PRESETS["Llama-3.3-70B"]


def test_paygo_multiplier_by_deployment_type():
    # Global is the base; Data Zone and Regional Standard are exactly 10% higher.
    assert paygo_multiplier("Global") == 1.0
    assert paygo_multiplier("Data Zone") == 1.10
    assert paygo_multiplier("Regional") == 1.10
    # Unknown/Custom type falls back to the base multiplier.
    assert paygo_multiplier("Custom") == 1.0


def test_paygo_rates_apply_confirmed_tier_delta():
    preset = MODEL_PRESETS["gpt-4.1"]
    # Global Standard base = confirmed pricing-page rates.
    assert paygo_rates(preset, "Global") == (2.0, 0.5, 8.0)
    # Data Zone and Regional Standard are 10% higher (confirmed gpt-4.1 values).
    assert paygo_rates(preset, "Data Zone") == (2.2, 0.55, 8.8)
    assert paygo_rates(preset, "Regional") == (2.2, 0.55, 8.8)


def test_paygo_rates_fall_back_to_defaults_for_custom():
    # A Custom/empty preset uses the editable DEFAULTS as the Global base.
    base = paygo_rates({}, "Global")
    assert base == (DEFAULTS["paygo_input_per_1m"], DEFAULTS["paygo_cached_per_1m"], DEFAULTS["paygo_output_per_1m"])


def test_data_zone_paygo_costs_more_than_global():
    # Same workload + model, only the deployment type differs: Data Zone PAYGO
    # must be exactly 10% above Global because every token rate is 10% higher.
    workload = {"avg_rpm": 100, "avg_input_tokens": 1500, "avg_output_tokens": 500, "p95_multiplier": 1.5, "cache_rate": 0.2}
    preset = MODEL_PRESETS["gpt-4.1"]
    g_in, g_cached, g_out = paygo_rates(preset, "Global")
    d_in, d_cached, d_out = paygo_rates(preset, "Data Zone")
    glob = calculate({**DEFAULTS, "paygo_input_per_1m": g_in, "paygo_cached_per_1m": g_cached, "paygo_output_per_1m": g_out, **workload})
    dz = calculate({**DEFAULTS, "paygo_input_per_1m": d_in, "paygo_cached_per_1m": d_cached, "paygo_output_per_1m": d_out, **workload})
    assert dz["paygo_monthly"] == pytest.approx(glob["paygo_monthly"] * 1.10)


def test_find_model_preset_exact_and_case_insensitive():
    name, preset = find_model_preset("gpt-4.1")
    assert name == "gpt-4.1"
    assert preset is MODEL_PRESETS["gpt-4.1"]
    # Case-insensitive match returns the canonical preset key.
    assert find_model_preset("GPT-4.1")[0] == "gpt-4.1"


def test_find_model_preset_trims_trailing_version():
    # Azure model names often carry a date version that is not in the preset key.
    assert find_model_preset("gpt-4.1-2025-04-14")[0] == "gpt-4.1"
    # Exact sub-variant still wins over its parent.
    assert find_model_preset("gpt-5-mini")[0] == "gpt-5-mini"
    assert find_model_preset("gpt-5-mini-2025-08-01")[0] == "gpt-5-mini"


def test_find_model_preset_unmatched_returns_none():
    assert find_model_preset("totally-unknown-model") == (None, {})
    assert find_model_preset("") == (None, {})
    assert find_model_preset(None) == (None, {})


def test_suggest_ptu_for_throughput_buffers_rounds_and_floors():
    # raw = 100000/3000 = 33.33; *1.15 = 38.33; round up to 5 -> 40.
    assert suggest_ptu_for_throughput(
        100000, model_tpm_per_ptu=3000, safety_buffer=0.15,
        min_ptu_commit=15, ptu_scale_increment=5,
    ) == 40
    # Tiny throughput is floored at the model minimum commitment.
    assert suggest_ptu_for_throughput(
        100, model_tpm_per_ptu=3000, min_ptu_commit=15, ptu_scale_increment=5,
    ) == 15


def test_suggest_ptu_for_throughput_uses_defaults_and_is_safe():
    # Missing params fall back to DEFAULTS; never divides by zero.
    val = suggest_ptu_for_throughput(0)
    assert val == DEFAULTS["min_ptu_commit"]
    big = suggest_ptu_for_throughput(1_000_000, model_tpm_per_ptu=0)  # guarded to 1
    assert math.isfinite(big) and big > 0


def test_build_report_html_includes_recommendation_and_cost_lanes():
    vals = {
        **DEFAULTS,
        **{k: MODEL_PRESETS["gpt-4.1"][k] for k in MODEL_PRESETS["gpt-4.1"] if k in DEFAULTS},
        "priority_input_per_1m": 3.5,
        "priority_cached_per_1m": 0.88,
        "priority_output_per_1m": 14.0,
        "priority_supported": True,
    }
    r = calculate(vals)
    html_out = ptu_core.build_report_html(
        vals, r, {"model": "gpt-4.1", "deployment_type": "Global", "region": "eastus2"}
    )
    assert html_out.startswith("<!doctype html>")
    assert html_out.rstrip().endswith("</html>")
    # Context + every cost lane is present.
    assert "gpt-4.1" in html_out and "eastus2" in html_out
    for lane in ("PTU (1-month reserved)", "PAYGO", "PTU + spillover", "Priority processing"):
        assert lane in html_out
    # Headline number renders.
    assert f'{r["recommended_ptu"]:,.0f}' in html_out
    # Disclaimer / pricing provenance carried through.
    assert ptu_core.PRICING_CONFIRMED_AS_OF in html_out
    assert "not an official Azure PTU calculator" in html_out


def test_build_report_html_marks_priority_not_applicable_when_unsupported():
    r = calculate({**DEFAULTS, "priority_supported": False})
    html_out = ptu_core.build_report_html(DEFAULTS, r, {"model": "gpt-4o"})
    # Priority lane shows n/a rather than a dollar figure.
    assert "Priority processing" in html_out
    assert "n/a" in html_out


def test_build_report_html_escapes_meta_to_prevent_injection():
    r = calculate(DEFAULTS)
    html_out = ptu_core.build_report_html(
        DEFAULTS, r, {"model": "<script>alert(1)</script>", "region": "x & y"}
    )
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out
    assert "x &amp; y" in html_out


def test_breakeven_series_returns_sorted_rows_and_lanes():
    be = ptu_core.breakeven_series(DEFAULTS, points=10)
    assert len(be["rows"]) == 10
    # RPM increases monotonically across the sweep.
    rpms = [row["rpm"] for row in be["rows"]]
    assert rpms == sorted(rpms)
    assert all(r > 0 for r in rpms)
    # Every row carries all four cost lanes.
    for row in be["rows"]:
        for key in ("paygo_monthly", "ptu_monthly", "blended_monthly", "priority_monthly"):
            assert key in row and math.isfinite(row[key])
    assert be["current_rpm"] == pytest.approx(DEFAULTS["avg_rpm"])


def test_breakeven_series_finds_crossover_where_ptu_overtakes_paygo():
    # A workload where the reserved PTU baseline is cheaper per token than PAYGO
    # (lower hourly price) so a crossover exists: PAYGO wins in the low-volume
    # floor region, PTU wins above break-even.
    vals = {**DEFAULTS, "ptu_hourly_price": 0.5, "avg_rpm": 120}
    be = ptu_core.breakeven_series(vals)
    assert be["breakeven_rpm"] is not None
    below = calculate({**vals, "avg_rpm": be["breakeven_rpm"] * 0.5})
    above = calculate({**vals, "avg_rpm": be["breakeven_rpm"] * 2.0})
    assert below["paygo_monthly"] < below["ptu_monthly"]
    assert above["paygo_monthly"] > above["ptu_monthly"]


def test_breakeven_series_returns_none_when_ptu_never_wins():
    # With the default $1/hr PTU price these assumptions keep PAYGO cheaper at
    # every volume, so there is no crossover to report.
    be = ptu_core.breakeven_series(DEFAULTS)
    assert be["breakeven_rpm"] is None


def test_breakeven_series_respects_explicit_rpm_max():
    be = ptu_core.breakeven_series(DEFAULTS, points=5, rpm_max=500)
    assert be["rows"][-1]["rpm"] == pytest.approx(500)


def test_breakeven_series_tier_selects_pricing_tier_for_ptu_lane():
    # The PTU lane should track the chosen pricing tier. At a fixed RPM the
    # hourly tier is the most expensive and the yearly tier the cheapest.
    vals = {**DEFAULTS, "avg_rpm": 200}
    hourly = ptu_core.breakeven_series(vals, points=4, ptu_tier="Hourly")
    monthly = ptu_core.breakeven_series(vals, points=4, ptu_tier="Monthly reservation")
    yearly = ptu_core.breakeven_series(vals, points=4, ptu_tier="Yearly reservation")
    assert hourly["ptu_tier"] == "Hourly"
    h = hourly["rows"][-1]["ptu_monthly"]
    m = monthly["rows"][-1]["ptu_monthly"]
    y = yearly["rows"][-1]["ptu_monthly"]
    assert h > m > y


def test_breakeven_series_cheaper_tier_lowers_breakeven():
    # A cheaper PTU tier should never push the crossover to a higher RPM than a
    # pricier tier; the yearly tier crosses no later than the monthly tier.
    vals = {**DEFAULTS, "ptu_hourly_price": 0.5, "avg_rpm": 120}
    monthly = ptu_core.breakeven_series(vals, ptu_tier="Monthly reservation")
    yearly = ptu_core.breakeven_series(vals, ptu_tier="Yearly reservation")
    assert monthly["breakeven_rpm"] is not None
    assert yearly["breakeven_rpm"] is not None
    assert yearly["breakeven_rpm"] <= monthly["breakeven_rpm"]


def test_build_report_csv_has_header_and_cost_lanes():
    r = calculate(DEFAULTS)
    csv_out = ptu_core.build_report_csv(
        DEFAULTS, r, {"model": "gpt-4.1", "deployment_type": "Global", "region": "eastus2"}
    )
    lines = csv_out.splitlines()
    assert lines[0] == "Section,Item,Value"
    # Every cost lane and the recommendation are present.
    for needle in ("PTU (1-month reserved)", "PAYGO", "PTU + spillover", "Priority processing", "Recommended PTUs"):
        assert needle in csv_out
    # Context carried through.
    assert "gpt-4.1" in csv_out and "eastus2" in csv_out
    # Parses cleanly as CSV with three columns per row.
    import csv as _csv
    rows = list(_csv.reader(csv_out.splitlines()))
    assert all(len(row) == 3 for row in rows)


def test_build_report_csv_marks_priority_not_applicable_when_unsupported():
    r = calculate({**DEFAULTS, "priority_supported": False})
    csv_out = ptu_core.build_report_csv(DEFAULTS, r, {"model": "gpt-4o"})
    assert "Priority processing,n/a" in csv_out


