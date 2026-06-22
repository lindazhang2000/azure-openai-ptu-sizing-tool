import pandas as pd
import streamlit as st

from ptu_core import DEFAULTS, MODEL_PRESETS, calculate

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
    preset_options = ["Custom"] + list(MODEL_PRESETS.keys())
    selected_model = st.selectbox(
        "Model preset",
        preset_options,
        index=preset_options.index("gpt-4.1"),
        help="Fills model throughput, output weighting, minimum commit, and scale increment from the official PTU sizing tables. Choose Custom to edit them freely.",
    )
    preset = MODEL_PRESETS.get(selected_model, {})

    foundry_mode = st.checkbox(
        "Match Foundry calculator (size for peak, no buffer)",
        value=False,
        help="Mirrors the official Foundry PTU calculator: treats RPM as the peak, with no baseline load factor and no safety buffer. Uncheck for the field-guidance baseline + spillover view.",
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        avg_rpm = st.number_input("Average RPM" if not foundry_mode else "Peak RPM", min_value=0.0, value=float(DEFAULTS["avg_rpm"]), step=1.0)
        avg_input_tokens = st.number_input("Average input tokens / request", min_value=0.0, value=float(DEFAULTS["avg_input_tokens"]), step=1.0)
    with c2:
        avg_output_tokens = st.number_input("Average output tokens / request", min_value=0.0, value=float(DEFAULTS["avg_output_tokens"]), step=1.0)
        p95_multiplier = st.slider("P95 load multiplier", min_value=1.0, max_value=5.0, value=1.0 if foundry_mode else float(DEFAULTS["p95_multiplier"]), step=0.1, disabled=foundry_mode)
    with c3:
        cache_rate = st.slider("Prompt cache rate", min_value=0.0, max_value=0.9, value=float(DEFAULTS["cache_rate"]), step=0.05)
        baseline_load_factor = st.slider("Baseline load factor", min_value=0.4, max_value=1.0, value=1.0 if foundry_mode else float(DEFAULTS["baseline_load_factor"]), step=0.05, disabled=foundry_mode)
        peak_minutes_fraction = st.slider("Peak minutes fraction", min_value=0.0, max_value=1.0, value=float(DEFAULTS["peak_minutes_fraction"]), step=0.05, help="Share of minutes the workload runs at its P95 peak (vs. its average minute). Drives how much traffic spills to Standard in the blended cost.")

    with st.expander("Advanced assumptions", expanded=True):
        a1, a2, a3, a4, a5 = st.columns(5)
        with a1:
            model_tpm_per_ptu = st.number_input("Model TPM per PTU", min_value=1.0, value=float(preset.get("model_tpm_per_ptu", DEFAULTS["model_tpm_per_ptu"])), step=1.0, disabled=bool(preset))
        with a2:
            output_weight = st.number_input("Output weighting", min_value=0.0, value=float(preset.get("output_weight", DEFAULTS["output_weight"])), step=0.1, disabled=bool(preset))
        with a3:
            safety_buffer = st.number_input("Safety buffer", min_value=0.0, value=0.0 if foundry_mode else float(DEFAULTS["safety_buffer"]), step=0.01, format="%.2f", disabled=foundry_mode)
        with a4:
            min_ptu_commit = st.number_input("Minimum PTU commit", min_value=0.0, value=float(preset.get("min_ptu_commit", DEFAULTS["min_ptu_commit"])), step=1.0, disabled=bool(preset))
        with a5:
            ptu_scale_increment = st.number_input("PTU scale increment", min_value=1.0, value=float(preset.get("ptu_scale_increment", DEFAULTS["ptu_scale_increment"])), step=1.0, disabled=bool(preset))

    with st.expander("Cost assumptions", expanded=True):
        b1, b2, b3, b4 = st.columns(4)
        with b1:
            ptu_hourly_price = st.number_input("PTU hourly price (USD)", min_value=0.0, value=float(DEFAULTS["ptu_hourly_price"]), step=0.01)
        with b2:
            reservation_discount_monthly = st.slider("Monthly reservation discount", min_value=0.0, max_value=0.9, value=float(DEFAULTS["reservation_discount_monthly"]), step=0.01, help="Discount off the hourly PTU price for a 1-month Azure Reservation (~64% for gpt-5.x).")
        with b3:
            reservation_discount_yearly = st.slider("Yearly reservation discount", min_value=0.0, max_value=0.9, value=float(DEFAULTS["reservation_discount_yearly"]), step=0.01, help="Discount off the hourly PTU price for a 1-year Azure Reservation (~70% for gpt-5.x).")
        with b4:
            paygo_input_per_1m = st.number_input("PAYGO input / 1M tokens (USD)", min_value=0.0, value=float(DEFAULTS["paygo_input_per_1m"]), step=0.01)
        b5, b6, b7 = st.columns(3)
        with b5:
            paygo_cached_per_1m = st.number_input("PAYGO cached input / 1M (USD)", min_value=0.0, value=float(DEFAULTS["paygo_cached_per_1m"]), step=0.01, help="Cached prompt tokens are billed at a discounted rate, not free.")
        with b6:
            paygo_output_per_1m = st.number_input("PAYGO output / 1M tokens (USD)", min_value=0.0, value=float(DEFAULTS["paygo_output_per_1m"]), step=0.01)
        with b7:
            hours_per_month = st.number_input("Hours per month", min_value=1.0, value=float(DEFAULTS["hours_per_month"]), step=1.0)

values = {
    "avg_rpm": avg_rpm,
    "avg_input_tokens": avg_input_tokens,
    "avg_output_tokens": avg_output_tokens,
    "p95_multiplier": p95_multiplier,
    "peak_minutes_fraction": peak_minutes_fraction,
    "cache_rate": cache_rate,
    "model_tpm_per_ptu": model_tpm_per_ptu,
    "output_weight": output_weight,
    "baseline_load_factor": baseline_load_factor,
    "safety_buffer": safety_buffer,
    "min_ptu_commit": min_ptu_commit,
    "ptu_scale_increment": ptu_scale_increment,
    "ptu_hourly_price": ptu_hourly_price,
    "reservation_discount_monthly": reservation_discount_monthly,
    "reservation_discount_yearly": reservation_discount_yearly,
    "paygo_input_per_1m": paygo_input_per_1m,
    "paygo_cached_per_1m": paygo_cached_per_1m,
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
m1, m2, m3, m4 = st.columns(4)
m1.metric("PTU monthly (1-mo reserved)", f'${calc["ptu_monthly"]:,.0f}', help=f'Hourly list: ${calc["ptu_hourly_monthly"]:,.0f}/mo before any reservation discount.')
m2.metric("PAYGO monthly", f'${calc["paygo_monthly"]:,.0f}')
m3.metric("PTU + spillover", f'${calc["blended_monthly"]:,.0f}', help=f'Reserved PTU baseline plus PAYGO for the ~{calc["spill_fraction"]*100:,.1f}% of monthly demand that exceeds provisioned capacity, given the peak-minutes duty cycle.')
delta_label = "PTU saves" if calc["savings_delta"] >= 0 else "PAYGO saves"
m4.metric(delta_label, f'${abs(calc["savings_delta"]):,.0f}')

pricing_df = pd.DataFrame([
    {
        "Term": t["term"],
        "Per PTU / month": f'${t["per_ptu_monthly"]:,.0f}',
        "Estimated monthly cost": f'${t["total_monthly"]:,.0f}',
        "Savings vs hourly": "-" if t["savings"] == 0 else f'{t["savings"]*100:,.0f}%',
    }
    for t in calc["pricing_tiers"]
])
st.caption(f'PTU pricing tiers (for {calc["recommended_ptu"]:,.0f} PTUs) — same layout as the Foundry PTU calculator')
st.dataframe(pricing_df, use_container_width=True, hide_index=True)

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
