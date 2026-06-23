import altair as alt
import pandas as pd
import streamlit as st

from ptu_core import DEFAULTS, DEPLOYMENT_TYPES, MODEL_PRESETS, available_deployment_types, available_regions, calculate, deployment_hourly_price, deployment_minimums, paygo_rates, region_data_source, region_supported, spillover_supported

st.set_page_config(page_title="Azure OpenAI PTU Sizing & Architecture Guidance Tool", page_icon="⚡", layout="wide")

st.title("Azure OpenAI PTU Sizing & Architecture Guidance Tool")
st.caption("Indicative workshop tool for PTU discovery, cost comparison, and architecture recommendations.")

# Widgets whose defaults are derived from the model preset, deployment type, or
# Foundry mode. These refresh automatically whenever one of those controls changes.
DEPENDENT_KEYS = [
    "p95_multiplier",
    "baseline_load_factor",
    "safety_buffer",
    "model_tpm_per_ptu",
    "output_weight",
    "min_ptu_commit",
    "ptu_scale_increment",
    "ptu_hourly_price",
    "paygo_input_per_1m",
    "paygo_cached_per_1m",
    "paygo_output_per_1m",
]


def compute_defaults(selected_model, deployment_type, foundry_mode):
    """Default value for every editable widget, given the current control selections."""
    preset = MODEL_PRESETS.get(selected_model, {})
    eff_min_ptu, eff_increment = deployment_minimums(preset, deployment_type)
    paygo_in, paygo_cached, paygo_out = paygo_rates(preset, deployment_type)
    return {
        # Free inputs (only restored by the Reset button).
        "avg_rpm": float(DEFAULTS["avg_rpm"]),
        "avg_input_tokens": float(DEFAULTS["avg_input_tokens"]),
        "avg_output_tokens": float(DEFAULTS["avg_output_tokens"]),
        "cache_rate": float(DEFAULTS["cache_rate"]),
        "peak_minutes_fraction": float(DEFAULTS["peak_minutes_fraction"]),
        "reservation_discount_monthly": float(DEFAULTS["reservation_discount_monthly"]),
        "reservation_discount_yearly": float(DEFAULTS["reservation_discount_yearly"]),
        "hours_per_month": float(DEFAULTS["hours_per_month"]),
        # Foundry-mode derived.
        "p95_multiplier": 1.0 if foundry_mode else float(DEFAULTS["p95_multiplier"]),
        "baseline_load_factor": 1.0 if foundry_mode else float(DEFAULTS["baseline_load_factor"]),
        "safety_buffer": 0.0 if foundry_mode else float(DEFAULTS["safety_buffer"]),
        # Preset derived.
        "model_tpm_per_ptu": float(preset.get("model_tpm_per_ptu", DEFAULTS["model_tpm_per_ptu"])),
        "output_weight": float(preset.get("output_weight", DEFAULTS["output_weight"])),
        # Deployment-type derived.
        "min_ptu_commit": float(eff_min_ptu),
        "ptu_scale_increment": float(eff_increment),
        "ptu_hourly_price": float(deployment_hourly_price(deployment_type)),
        # Preset + deployment-type derived.
        "paygo_input_per_1m": float(paygo_in),
        "paygo_cached_per_1m": float(paygo_cached),
        "paygo_output_per_1m": float(paygo_out),
    }


_INITIAL_MODEL = "gpt-4.1"
_INITIAL_DEPLOYMENT = available_deployment_types(MODEL_PRESETS.get(_INITIAL_MODEL, {}), _INITIAL_MODEL)[0]

if "_prev_controls" not in st.session_state:
    for _k, _v in compute_defaults(_INITIAL_MODEL, _INITIAL_DEPLOYMENT, False).items():
        st.session_state.setdefault(_k, _v)
    st.session_state.setdefault("selected_model", _INITIAL_MODEL)
    st.session_state.setdefault("deployment_type", _INITIAL_DEPLOYMENT)
    st.session_state.setdefault("region", available_regions(_INITIAL_MODEL, _INITIAL_DEPLOYMENT)[0])
    st.session_state.setdefault("foundry_mode", False)
    st.session_state["_prev_controls"] = (_INITIAL_MODEL, _INITIAL_DEPLOYMENT, False)


def _reset_defaults():
    """Restore every assumption to its default for the current model/deployment/mode."""
    defaults = compute_defaults(
        st.session_state["selected_model"],
        st.session_state["deployment_type"],
        st.session_state["foundry_mode"],
    )
    for k, v in defaults.items():
        st.session_state[k] = v


with st.sidebar:
    st.header("Quick actions")
    st.button("Reset to default assumptions", on_click=_reset_defaults)
    st.markdown("**Note**  \nThis tool provides **illustrative and directional guidance only** and is **not an official Azure PTU calculator**. Throughput assumptions, minimum PTU commitments, and pricing are subject to change. Always verify against current Azure documentation before making customer-specific decisions.")

left, right = st.columns([1.25, 0.75], gap="large")

with left:
    st.subheader("Workload inputs")
    preset_options = ["Custom"] + list(MODEL_PRESETS.keys())
    selected_model = st.selectbox(
        "Model preset",
        preset_options,
        key="selected_model",
        help="Fills model throughput, output weighting, minimum commit, and scale increment from the official PTU sizing tables. Choose Custom to edit them freely.",
    )
    preset = MODEL_PRESETS.get(selected_model, {})

    deployment_options = available_deployment_types(preset, selected_model)
    if st.session_state["deployment_type"] not in deployment_options:
        st.session_state["deployment_type"] = deployment_options[0]
    deployment_type = st.selectbox(
        "Deployment type",
        deployment_options,
        key="deployment_type",
        help="Global and Data Zone provisioned share the lower minimum (e.g. 15 PTUs) and a 5-PTU scale increment. Regional provisioned uses larger model-specific minimums (e.g. 50 PTUs, 50 increment). Only the deployment types each model supports are listed; availability also varies by region — see the Microsoft Learn references.",
    )
    if spillover_supported(deployment_type):
        st.caption(f"✅ {deployment_type} provisioned supports automatic spillover to a matching Standard deployment (preview).")
    else:
        st.caption(f"⚠️ {deployment_type} provisioned does not support automatic spillover — use Global or Data Zone for that, or route overflow manually.")

    region_options = available_regions(selected_model, deployment_type)
    _region_choices = region_options if region_options else ["(none listed)"]
    if st.session_state["region"] not in _region_choices:
        st.session_state["region"] = _region_choices[0]
    _region_src, _region_asof = region_data_source()
    _region_label = "Region (live)" if _region_src == "live" else "Region (indicative)"
    region = st.selectbox(
        _region_label,
        _region_choices,
        key="region",
        help="Azure regions where this model + deployment type is offered for provisioned throughput. When live data is loaded (region_data.json), this comes straight from the Azure Models API; otherwise it is an indicative built-in subset. Global routes across regions; Data Zone stays within the US/EU zone; Regional pins to a single region. Availability changes frequently — confirm against the live Microsoft Learn region tables.",
    )
    if _region_src == "live":
        _asof = (_region_asof or "")[:10]
        if region_options:
            st.caption(f"✅ {len(region_options)} region(s) for {selected_model} · {deployment_type} — live from Azure Models API (refreshed {_asof}).")
        else:
            st.caption(f"⚠️ {selected_model} · {deployment_type} not offered per the Azure Models API (refreshed {_asof}). Try another deployment type or region.")
    else:
        if region_options:
            st.caption(f"{len(region_options)} indicative region(s) for {selected_model} · {deployment_type}. Verify against the live region tables before deployment.")
        else:
            st.caption(f"No indicative regions listed for {selected_model} · {deployment_type}. Confirm availability in the Microsoft Learn region tables.")

    foundry_mode = st.checkbox(
        "Match Foundry calculator (size for peak, no buffer)",
        key="foundry_mode",
        help="Mirrors the official Foundry PTU calculator: treats RPM as the peak, with no baseline load factor and no safety buffer. Uncheck for the field-guidance baseline + spillover view.",
    )

    # When the model preset, deployment type, or Foundry mode changes, refresh the
    # widgets derived from them. Free inputs are left untouched so manual edits
    # survive a preset switch; the Reset button restores everything.
    _controls = (selected_model, deployment_type, foundry_mode)
    if st.session_state["_prev_controls"] != _controls:
        _fresh = compute_defaults(selected_model, deployment_type, foundry_mode)
        for _key in DEPENDENT_KEYS:
            st.session_state[_key] = _fresh[_key]
        st.session_state["_prev_controls"] = _controls

    c1, c2, c3 = st.columns(3)
    with c1:
        avg_rpm = st.number_input("Average RPM" if not foundry_mode else "Peak RPM", min_value=0.0, step=1.0, key="avg_rpm")
        avg_input_tokens = st.number_input("Average input tokens / request", min_value=0.0, step=1.0, key="avg_input_tokens")
    with c2:
        avg_output_tokens = st.number_input("Average output tokens / request", min_value=0.0, step=1.0, key="avg_output_tokens")
        p95_multiplier = st.slider("P95 load multiplier", min_value=1.0, max_value=5.0, step=0.1, disabled=foundry_mode, key="p95_multiplier")
    with c3:
        cache_rate = st.slider("Prompt cache rate", min_value=0.0, max_value=0.9, step=0.05, key="cache_rate")
        baseline_load_factor = st.slider("Baseline load factor", min_value=0.4, max_value=1.0, step=0.05, disabled=foundry_mode, key="baseline_load_factor")
        peak_minutes_fraction = st.slider("Peak minutes fraction", min_value=0.0, max_value=1.0, step=0.05, key="peak_minutes_fraction", help="Share of minutes the workload runs at its P95 peak (vs. its average minute). Drives how much traffic spills to Standard in the blended cost.")

    with st.expander("Advanced assumptions", expanded=True):
        a1, a2, a3, a4, a5 = st.columns(5)
        with a1:
            model_tpm_per_ptu = st.number_input("Model TPM per PTU", min_value=1.0, step=1.0, disabled=bool(preset), key="model_tpm_per_ptu")
        with a2:
            output_weight = st.number_input("Output weighting", min_value=0.0, step=0.1, disabled=bool(preset), key="output_weight")
        with a3:
            safety_buffer = st.number_input("Safety buffer", min_value=0.0, step=0.01, format="%.2f", disabled=foundry_mode, key="safety_buffer")
        with a4:
            min_ptu_commit = st.number_input("Minimum PTU commit", min_value=0.0, step=1.0, disabled=bool(preset), key="min_ptu_commit")
        with a5:
            ptu_scale_increment = st.number_input("PTU scale increment", min_value=1.0, step=1.0, disabled=bool(preset), key="ptu_scale_increment")

    with st.expander("Cost assumptions", expanded=True):
        st.caption("Hourly price is differentiated by deployment type (Global lowest → Data Zone → Regional); reservation prices do not vary by type. Hourly/reservation values confirmed against the Azure OpenAI pricing page (Provisioned table). PAYGO defaults track the selected model **and** deployment type — Global Standard base, with Data Zone/Regional Standard exactly 10% higher (confirmed). Re-verify before quoting.")
        b1, b2, b3, b4 = st.columns(4)
        with b1:
            ptu_hourly_price = st.number_input("PTU hourly price (USD)", min_value=0.0, step=0.01, key="ptu_hourly_price", help=f"{deployment_type} provisioned hourly $/PTU. Global $1.00, Data Zone $1.10, Regional $2.00 (confirmed).")
        with b2:
            reservation_discount_monthly = st.slider("Monthly reservation discount", min_value=0.0, max_value=0.9, step=0.01, key="reservation_discount_monthly", help="Discount off the hourly PTU price for a 1-month Azure Reservation ($260/PTU/mo vs $730 hourly-equivalent = ~64%).")
        with b3:
            reservation_discount_yearly = st.slider("Yearly reservation discount", min_value=0.0, max_value=0.9, step=0.01, key="reservation_discount_yearly", help="Discount off the hourly PTU price for a 1-year Azure Reservation ($2,652/PTU/yr = $221/mo = ~70%).")
        with b4:
            paygo_input_per_1m = st.number_input("PAYGO input / 1M tokens (USD)", min_value=0.0, step=0.01, key="paygo_input_per_1m", help=f"{deployment_type} Standard input rate. Global base; Data Zone/Regional are 10% higher (confirmed).")
        b5, b6, b7 = st.columns(3)
        with b5:
            paygo_cached_per_1m = st.number_input("PAYGO cached input / 1M (USD)", min_value=0.0, step=0.01, key="paygo_cached_per_1m", help="Cached prompt tokens are billed at a discounted rate, not free.")
        with b6:
            paygo_output_per_1m = st.number_input("PAYGO output / 1M tokens (USD)", min_value=0.0, step=0.01, key="paygo_output_per_1m")
        with b7:
            hours_per_month = st.number_input("Hours per month", min_value=1.0, step=1.0, key="hours_per_month")

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
    "spillover_supported": spillover_supported(deployment_type),
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
m3.metric("PTU + spillover", f'${calc["blended_monthly"]:,.0f}', delta=f'{calc["spill_fraction"]*100:,.1f}% on Standard', delta_color="off", help=f'Reserved PTU baseline plus PAYGO for the ~{calc["spill_fraction"]*100:,.1f}% of monthly demand that exceeds provisioned capacity, given the peak-minutes duty cycle.')
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
ptu_axis_max = max(calc["recommended_ptu"], calc["peak_reference_ptu"], 1) * 1.15
ptu_chart = (
    alt.Chart(chart_df)
    .mark_bar()
    .encode(
        x=alt.X("Scenario:N", sort=["Baseline PTU", "Peak ref PTU"], title=None),
        y=alt.Y("PTUs:Q", title="PTUs", stack=None, scale=alt.Scale(domain=[0, ptu_axis_max])),
        tooltip=["Scenario", "PTUs"],
    )
)
st.altair_chart(ptu_chart, use_container_width=True)

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
    "Designed for illustrative and directional sizing guidance. It reflects the same field guidance used in your PTU playbook: size PTU for the steady-state baseline, use Standard or PAYGO for spillover, and treat reservation as a billing optimization after validation."
)
