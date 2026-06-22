import pandas as pd
import streamlit as st

from ptu_core import DEFAULTS, calculate

st.set_page_config(page_title="PTU Sizing Demo", page_icon="⚡", layout="wide")

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
        avg_rpm = st.number_input("Average RPM", min_value=0.0, value=float(DEFAULTS["avg_rpm"]), step=1.0)
        avg_input_tokens = st.number_input("Average input tokens / request", min_value=0.0, value=float(DEFAULTS["avg_input_tokens"]), step=1.0)
    with c2:
        avg_output_tokens = st.number_input("Average output tokens / request", min_value=0.0, value=float(DEFAULTS["avg_output_tokens"]), step=1.0)
        p95_multiplier = st.slider("P95 load multiplier", min_value=1.0, max_value=5.0, value=float(DEFAULTS["p95_multiplier"]), step=0.1)
    with c3:
        cache_rate = st.slider("Prompt cache rate", min_value=0.0, max_value=0.9, value=float(DEFAULTS["cache_rate"]), step=0.05)
        baseline_load_factor = st.slider("Baseline load factor", min_value=0.4, max_value=1.0, value=float(DEFAULTS["baseline_load_factor"]), step=0.05)

    with st.expander("Advanced assumptions", expanded=True):
        a1, a2, a3, a4 = st.columns(4)
        with a1:
            model_tpm_per_ptu = st.number_input("Model TPM per PTU", min_value=1.0, value=float(DEFAULTS["model_tpm_per_ptu"]), step=1.0)
        with a2:
            output_weight = st.number_input("Output weighting", min_value=0.0, value=float(DEFAULTS["output_weight"]), step=0.1)
        with a3:
            safety_buffer = st.number_input("Safety buffer", min_value=0.0, value=float(DEFAULTS["safety_buffer"]), step=0.01, format="%.2f")
        with a4:
            min_ptu_commit = st.number_input("Minimum PTU commit", min_value=0.0, value=float(DEFAULTS["min_ptu_commit"]), step=1.0)

    with st.expander("Cost assumptions", expanded=True):
        b1, b2, b3, b4 = st.columns(4)
        with b1:
            ptu_hourly_price = st.number_input("PTU hourly price (USD)", min_value=0.0, value=float(DEFAULTS["ptu_hourly_price"]), step=0.01)
        with b2:
            paygo_input_per_1m = st.number_input("PAYGO input / 1M tokens (USD)", min_value=0.0, value=float(DEFAULTS["paygo_input_per_1m"]), step=0.01)
        with b3:
            paygo_output_per_1m = st.number_input("PAYGO output / 1M tokens (USD)", min_value=0.0, value=float(DEFAULTS["paygo_output_per_1m"]), step=0.01)
        with b4:
            hours_per_month = st.number_input("Hours per month", min_value=1.0, value=float(DEFAULTS["hours_per_month"]), step=1.0)

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
    st.metric("Burst ratio (P95 / average)", f'{calc["burst_ratio"]:,.2f}x')

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
