import math
import pandas as pd
import streamlit as st

st.set_page_config(page_title="PTU Sizing Demo", page_icon="⚡", layout="wide")

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

st.title("PTU Sizing Demo")
st.caption("Indicative workshop tool for PTU discovery, cost comparison, and architecture recommendations.")

with st.sidebar:
    st.header("Quick actions")
    if st.button("Reset to default assumptions"):
        for k, v in DEFAULTS.items():
            st.session_state[k] = v
    st.markdown("**Note**  \nThis is a workshop/demo artifact. Replace model throughput, minimums, and pricing with validated customer-specific values before external use.")

left, right = st.columns([1.25, 0.75], gap="large")

with left:
    st.subheader("Workload inputs")
    c1, c2, c3 = st.columns(3)
    with c1:
        avg_rpm = st.number_input("Average RPM", min_value=0.0, value=DEFAULTS["avg_rpm"], step=1.0)
        avg_input_tokens = st.number_input("Average input tokens / request", min_value=0.0, value=DEFAULTS["avg_input_tokens"], step=1.0)
    with c2:
        avg_output_tokens = st.number_input("Average output tokens / request", min_value=0.0, value=DEFAULTS["avg_output_tokens"], step=1.0)
        p95_multiplier = st.slider("P95 load multiplier", min_value=1.0, max_value=5.0, value=float(DEFAULTS["p95_multiplier"]), step=0.1)
    with c3:
        cache_rate = st.slider("Prompt cache rate", min_value=0.0, max_value=0.9, value=float(DEFAULTS["cache_rate"]), step=0.05)
        baseline_load_factor = st.slider("Baseline load factor", min_value=0.4, max_value=1.0, value=float(DEFAULTS["baseline_load_factor"]), step=0.05)

    with st.expander("Advanced assumptions", expanded=True):
        a1, a2, a3, a4 = st.columns(4)
        with a1:
            model_tpm_per_ptu = st.number_input("Model TPM per PTU", min_value=1.0, value=DEFAULTS["model_tpm_per_ptu"], step=1.0)
        with a2:
            output_weight = st.number_input("Output weighting", min_value=0.0, value=float(DEFAULTS["output_weight"]), step=0.1)
        with a3:
            safety_buffer = st.number_input("Safety buffer", min_value=0.0, value=float(DEFAULTS["safety_buffer"]), step=0.01, format="%.2f")
        with a4:
            min_ptu_commit = st.number_input("Minimum PTU commit", min_value=0.0, value=DEFAULTS["min_ptu_commit"], step=1.0)

    with st.expander("Cost assumptions", expanded=True):
        b1, b2, b3, b4 = st.columns(4)
        with b1:
            ptu_hourly_price = st.number_input("PTU hourly price (USD)", min_value=0.0, value=float(DEFAULTS["ptu_hourly_price"]), step=0.01)
        with b2:
            paygo_input_per_1m = st.number_input("PAYGO input / 1M tokens (USD)", min_value=0.0, value=float(DEFAULTS["paygo_input_per_1m"]), step=0.01)
        with b3:
            paygo_output_per_1m = st.number_input("PAYGO output / 1M tokens (USD)", min_value=0.0, value=float(DEFAULTS["paygo_output_per_1m"]), step=0.01)
        with b4:
            hours_per_month = st.number_input("Hours per month", min_value=1.0, value=DEFAULTS["hours_per_month"], step=1.0)

values = {
    "avg_rpm": avg_rpm,
    "avg_input_tokens": avg_input_tokens,
    "avg_output_tokens": avg_output_tokens,
    "p95_multiplier": p95_multiplier,
    "cache_rate": cache_rate,
    "model_tpm_per_ptu": model_tpm_per_ptu,
    "output_weight": output_weight,
    "baseline_load_factor": baseline_load_factor,
    "safety_buffer": safety_buffer,
    "min_ptu_commit": min_ptu_commit,
    "ptu_hourly_price": ptu_hourly_price,
    "paygo_input_per_1m": paygo_input_per_1m,
    "paygo_output_per_1m": paygo_output_per_1m,
    "hours_per_month": hours_per_month,
}
calc = calculate(values)

with right:
    st.subheader("Outputs")
    k1, k2 = st.columns(2)
    k1.metric("Recommended PTUs", f'{calc["recommended_ptu"]:,.0f}')
    k2.metric("Peak reference PTUs", f'{calc["peak_reference_ptu"]:,.0f}')

    st.metric("Baseline TPM", f'{calc["baseline_tpm"]:,.0f}')
    st.metric("P95 TPM", f'{calc["p95_tpm"]:,.0f}')
    st.metric("Burst ratio (P95 / baseline)", f'{calc["burst_ratio"]:,.2f}x')

    st.markdown(f"### {calc['architecture']['badge']} {calc['architecture']['label']}")
    st.write(calc['architecture']['summary'])
    st.caption(calc['architecture']['reason'])
    st.info(calc['reservation_note'])

st.subheader("Monthly cost comparison")
m1, m2, m3 = st.columns(3)
m1.metric("PTU monthly", f'${calc["ptu_monthly"]:,.0f}')
m2.metric("PAYGO monthly", f'${calc["paygo_monthly"]:,.0f}')
delta_label = "PTU saves" if calc["savings_delta"] >= 0 else "PAYGO saves"
m3.metric(delta_label, f'${abs(calc["savings_delta"]):,.0f}')

chart_df = pd.DataFrame([
    {"Scenario": "Baseline PTU", "PTUs": calc["recommended_ptu"]},
    {"Scenario": "Peak ref PTU", "PTUs": calc["peak_reference_ptu"]},
])
st.bar_chart(chart_df.set_index("Scenario"))

summary_df = pd.DataFrame([
    ["Average input-equivalent TPM", calc["avg_tpm"]],
    ["P95 input-equivalent TPM", calc["p95_tpm"]],
    ["Baseline input-equivalent TPM", calc["baseline_tpm"]],
    ["Monthly requests", calc["monthly_requests"]],
    ["Monthly input tokens (effective)", calc["input_tokens_monthly"]],
    ["Monthly output tokens", calc["output_tokens_monthly"]],
], columns=["Metric", "Value"])
summary_df["Value"] = summary_df["Value"].map(lambda x: f"{x:,.0f}")
st.subheader("Calculation summary")
st.dataframe(summary_df, use_container_width=True, hide_index=True)

st.markdown("---")
st.caption(
    "Designed as an indicative workshop/demo artifact. It reflects the same field guidance used in your PTU playbook: size PTU for the steady-state baseline, use Standard or PAYGO for spillover, and treat reservation as a billing optimization after validation."
)
