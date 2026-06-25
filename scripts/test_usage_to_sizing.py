"""Unit tests for the usage -> sizing-inputs bridge (usage_to_sizing.py).

These exercise the pure mapping (usage_to_inputs) and the report iteration without
touching Azure: a tiny synthetic report drives the math, and ptu_core is imported
the same way the script does.
"""

import token_usage as tu
import usage_to_sizing as u2s


CORE = tu._ptu_core()


def _report():
    """A minimal two-deployment report over a 1-day, PT1H window."""
    return {
        "period": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z",
                   "interval": "PT1H"},
        "interval_minutes": 60.0,
        "accounts": [
            {
                "name": "acct-a", "kind": "AIServices", "resourceGroup": "rg",
                "location": "eastus2",
                "deployments": {
                    "dep-chat": {
                        "model": "gpt-4.1", "version": "2025-04-14",
                        # 1 day = 1440 min. prompt 1,440,000 -> 1000/min input.
                        "totals": {"prompt_tokens": 1_440_000,
                                   "generated_tokens": 720_000,
                                   "total_tokens": 2_160_000},
                        # peak hour 180,000 total -> 3000/min; avg total 1500/min -> burst 2.0
                        "peak": {"tokens": 180_000, "time": "2026-06-01T14:00:00Z"},
                    },
                },
            },
            {
                "name": "acct-b", "kind": "OpenAI", "resourceGroup": "rg",
                "location": "westus",
                "deployments": {
                    "dep-edge": {
                        "model": "some-unknown-model", "version": "1",
                        "totals": {"prompt_tokens": 0, "generated_tokens": 0,
                                   "total_tokens": 0},
                        "peak": {"tokens": 0, "time": ""},
                    },
                },
            },
        ],
    }


def test_period_minutes_one_day():
    assert u2s._period_minutes(_report()) == 1440.0


def test_usage_to_inputs_derives_throughput_and_burst():
    dep = _report()["accounts"][0]["deployments"]["dep-chat"]
    vals = u2s.usage_to_inputs(dep, 1440.0, 60.0, core=CORE, avg_rpm=60.0)
    obs = vals["_observed"]
    assert obs["avg_input_tpm"] == 1000.0          # 1,440,000 / 1440
    assert obs["avg_total_tpm"] == 1500.0          # 2,160,000 / 1440
    assert obs["peak_total_tpm"] == 3000.0         # 180,000 / 60
    assert obs["burst_ratio"] == 2.0               # 3000 / 1500
    assert vals["p95_multiplier"] == 2.0
    # Per-request sizes are throughput / avg_rpm.
    assert vals["avg_input_tokens"] == 1000.0 / 60.0
    assert vals["avg_output_tokens"] == 500.0 / 60.0
    # gpt-4.1 preset is matched.
    assert vals["_preset"] == "gpt-4.1"
    assert vals["model_tpm_per_ptu"] == CORE.MODEL_PRESETS["gpt-4.1"]["model_tpm_per_ptu"]


def test_usage_to_inputs_rpm_does_not_change_recommendation():
    """The recommended PTU must be independent of the nominal --avg-rpm."""
    dep = _report()["accounts"][0]["deployments"]["dep-chat"]
    a = CORE.calculate(u2s._public_inputs(
        u2s.usage_to_inputs(dep, 1440.0, 60.0, core=CORE, avg_rpm=10.0)))
    b = CORE.calculate(u2s._public_inputs(
        u2s.usage_to_inputs(dep, 1440.0, 60.0, core=CORE, avg_rpm=600.0)))
    assert a["recommended_ptu"] == b["recommended_ptu"]
    assert a["avg_tpm"] == b["avg_tpm"]


def test_usage_to_inputs_unmatched_model_uses_defaults():
    dep = _report()["accounts"][1]["deployments"]["dep-edge"]
    vals = u2s.usage_to_inputs(dep, 1440.0, 60.0, core=CORE)
    assert vals["_preset"] is None
    assert vals["model_tpm_per_ptu"] == CORE.DEFAULTS["model_tpm_per_ptu"]
    # No traffic -> burst floored at 1.0, no divide-by-zero.
    assert vals["_observed"]["burst_ratio"] == 1.0


def test_regional_uses_regional_minimums():
    dep = _report()["accounts"][0]["deployments"]["dep-chat"]
    g = u2s.usage_to_inputs(dep, 1440.0, 60.0, core=CORE, deployment_type="Global")
    r = u2s.usage_to_inputs(dep, 1440.0, 60.0, core=CORE, deployment_type="Regional")
    preset = CORE.MODEL_PRESETS["gpt-4.1"]
    assert g["min_ptu_commit"] == preset["min_ptu_commit"]
    assert r["min_ptu_commit"] == preset["regional_min_ptu_commit"]
    assert r["ptu_scale_increment"] == preset["regional_ptu_scale_increment"]


def test_iter_deployments_filters():
    report = _report()
    all_deps = list(u2s._iter_deployments(report, None, None))
    assert len(all_deps) == 2
    only_a = list(u2s._iter_deployments(report, "acct-a", None))
    assert len(only_a) == 1 and only_a[0][1] == "dep-chat"
    only_chat = list(u2s._iter_deployments(report, None, "chat"))
    assert len(only_chat) == 1


def test_public_inputs_strips_private_keys():
    dep = _report()["accounts"][0]["deployments"]["dep-chat"]
    vals = u2s.usage_to_inputs(dep, 1440.0, 60.0, core=CORE)
    pub = u2s._public_inputs(vals)
    assert not any(k.startswith("_") for k in pub)
    # The public dict must be accepted by calculate.
    assert CORE.calculate(pub)["recommended_ptu"] > 0


def test_main_demo_runs_end_to_end(capsys):
    rc = u2s.main(["--demo", "--days", "2", "--interval", "PT1H", "--calculate"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage -> sizing inputs" in out
    assert "PTU baseline" in out
