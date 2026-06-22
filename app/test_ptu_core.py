import math

import pytest

from ptu_core import DEFAULTS, calculate


def test_defaults_match_expected_scenario():
    r = calculate(DEFAULTS)

    # Throughput proxy: 60 * (1800*0.8 + 650*4) = 60 * (1440 + 2600) = 242400
    assert r["avg_tpm"] == pytest.approx(242400.0)
    assert r["p95_tpm"] == pytest.approx(242400.0 * 1.8)              # 436320
    assert r["baseline_tpm"] == pytest.approx(242400.0 * 1.8 * 0.70)  # 305424

    # Sizing: raw = 305424/3000 = 101.808; *1.15 = 117.0792 -> ceil 118
    assert r["raw_baseline_ptu"] == pytest.approx(101.808)
    assert r["recommended_ptu"] == 118
    # peak: (436320/3000)*1.15 = 167.256 -> ceil 168
    assert r["peak_reference_ptu"] == 168

    # burst_ratio = p95/baseline = 1 / baseline_load_factor
    assert r["burst_ratio"] == pytest.approx(1 / 0.70)

    # Cost
    assert r["ptu_monthly"] == pytest.approx(118 * 15.0 * 730)
    assert r["savings_delta"] == pytest.approx(r["paygo_monthly"] - r["ptu_monthly"])


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
    "load,expected_label",
    [
        (1.0, "PTU-first production baseline"),   # burst_ratio = 1.0  (<2)
        (0.40, "PTU + Standard spillover"),       # burst_ratio = 2.5  (2..4)
        (0.20, "PAYGO or smaller PTU pilot"),     # burst_ratio = 5.0  (>=4)
    ],
)
def test_architecture_recommendation_by_burstiness(load, expected_label):
    # burst_ratio depends only on baseline_load_factor (= 1 / load)
    vals = {**DEFAULTS, "baseline_load_factor": load}
    r = calculate(vals)
    assert r["burst_ratio"] == pytest.approx(1 / load)
    assert r["architecture"]["label"] == expected_label
