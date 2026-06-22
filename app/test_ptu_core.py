import math

import pytest

from ptu_core import DEFAULTS, MODEL_PRESETS, calculate


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

    # Cost: reservation discount 0 -> reserved == hourly
    assert r["ptu_monthly"] == pytest.approx(120 * 15.0 * 730)
    assert r["ptu_hourly_monthly"] == pytest.approx(120 * 15.0 * 730)
    assert r["ptu_reserved_monthly"] == pytest.approx(r["ptu_monthly"])
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


def test_reservation_discount_reduces_ptu_cost():
    full = calculate({**DEFAULTS, "reservation_discount": 0.0})
    discounted = calculate({**DEFAULTS, "reservation_discount": 0.30})
    assert discounted["ptu_monthly"] == pytest.approx(full["ptu_monthly"] * 0.70)
    # Hourly list price is unaffected by the reservation discount
    assert discounted["ptu_hourly_monthly"] == pytest.approx(full["ptu_hourly_monthly"])


def test_blended_spillover_between_reserved_and_paygo():
    r = calculate(DEFAULTS)
    assert 0 <= r["spill_fraction"] <= 1
    # Blended = reserved baseline + a fraction of PAYGO -> never below reserved
    assert r["blended_monthly"] >= r["ptu_reserved_monthly"]
    assert r["blended_monthly"] == pytest.approx(
        r["ptu_reserved_monthly"] + r["spill_fraction"] * r["paygo_monthly"]
    )


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
