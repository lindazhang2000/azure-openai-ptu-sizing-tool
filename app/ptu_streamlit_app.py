import json
import os

import altair as alt
import pandas as pd
import streamlit as st

import ptu_core
from ptu_core import DEFAULTS, DEPLOYMENT_TYPES, MODEL_PRESETS, PRICING_CONFIRMED_AS_OF, PRICING_SOURCE_URL, available_deployment_types, available_regions, breakeven_series, build_report_csv, build_report_html, calculate, deployment_hourly_price, deployment_minimums, model_supports_priority, paygo_rates, priority_rates, priority_supported, region_data_source, region_supported, sensitivity_table, spillover_supported, validate_inputs

st.set_page_config(page_title="Azure OpenAI PTU Sizing & Architecture Guidance Tool", page_icon="⚡", layout="wide")


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_region_data_from_blob(blob_url):
    """Fetch region_data.json from Azure Blob Storage via managed identity / AAD.

    Returns the parsed dict, or ``None`` on any failure so the app falls back to
    the bundled ``region_data.json``. Cached for one hour to avoid repeated calls.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobClient

        credential = DefaultAzureCredential()
        client = BlobClient.from_blob_url(blob_url, credential=credential)
        raw = client.download_blob().readall()
        return json.loads(raw)
    except Exception:
        return None


# When REGION_DATA_BLOB_URL is set (e.g. on App Service), prefer the daily-refreshed
# blob over the bundled snapshot. Falls back silently to the bundled file otherwise.
_BLOB_URL = os.environ.get("REGION_DATA_BLOB_URL")
if _BLOB_URL:
    _blob_data = _fetch_region_data_from_blob(_BLOB_URL)
    if _blob_data:
        ptu_core.set_live_region_data(_blob_data)

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
    "priority_input_per_1m",
    "priority_cached_per_1m",
    "priority_output_per_1m",
]


def compute_defaults(selected_model, deployment_type, foundry_mode):
    """Default value for every editable widget, given the current control selections."""
    preset = MODEL_PRESETS.get(selected_model, {})
    eff_min_ptu, eff_increment = deployment_minimums(preset, deployment_type)
    paygo_in, paygo_cached, paygo_out = paygo_rates(preset, deployment_type)
    # Confirmed per-model priority rates when the model offers priority
    # processing; otherwise seed the fields from PAYGO x the multiplier fallback.
    prio = priority_rates(preset, deployment_type)
    if prio is None:
        _pm = float(DEFAULTS["priority_multiplier"])
        prio = (paygo_in * _pm, paygo_cached * _pm, paygo_out * _pm)
    prio_in, prio_cached, prio_out = prio
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
        "priority_input_per_1m": float(prio_in),
        "priority_cached_per_1m": float(prio_cached),
        "priority_output_per_1m": float(prio_out),
    }


_INITIAL_MODEL = "gpt-4.1"
_INITIAL_DEPLOYMENT = available_deployment_types(MODEL_PRESETS.get(_INITIAL_MODEL, {}), _INITIAL_MODEL)[0]

# Inputs that round-trip through the page URL so a sizing scenario can be shared
# or bookmarked. Controls and the foundry toggle are always encoded; numeric
# inputs are only encoded when they differ from the model/deployment default, to
# keep a pristine link short.
_SHARE_STR_KEYS = ("selected_model", "deployment_type", "region")
_SHARE_FLOAT_KEYS = (
    "avg_rpm", "avg_input_tokens", "avg_output_tokens", "p95_multiplier",
    "peak_minutes_fraction", "cache_rate", "baseline_load_factor", "safety_buffer",
    "hours_per_month", "reservation_discount_monthly", "reservation_discount_yearly",
    "model_tpm_per_ptu", "output_weight", "min_ptu_commit", "ptu_scale_increment",
    "ptu_hourly_price", "paygo_input_per_1m", "paygo_cached_per_1m", "paygo_output_per_1m",
    "priority_input_per_1m", "priority_cached_per_1m", "priority_output_per_1m",
)


def _hydrate_from_query_params():
    """Seed session state from a shared URL (validated), run once at first load."""
    qp = st.query_params
    valid_models = ["Custom"] + list(MODEL_PRESETS.keys())
    model = qp.get("selected_model")
    if model in valid_models:
        st.session_state["selected_model"] = model
    cur_model = st.session_state["selected_model"]
    preset = MODEL_PRESETS.get(cur_model, {})
    dep = qp.get("deployment_type")
    if dep in available_deployment_types(preset, cur_model):
        st.session_state["deployment_type"] = dep
    reg = qp.get("region")
    if reg and reg in available_regions(cur_model, st.session_state["deployment_type"]):
        st.session_state["region"] = reg
    fm = qp.get("foundry_mode")
    if fm is not None:
        st.session_state["foundry_mode"] = str(fm).lower() in ("1", "true", "yes")
    for _k in _SHARE_FLOAT_KEYS:
        raw = qp.get(_k)
        if raw is None:
            continue
        try:
            st.session_state[_k] = float(raw)
        except (TypeError, ValueError):
            pass


def _sync_query_params():
    """Mirror the current inputs into the URL so it stays shareable/bookmarkable."""
    defaults = compute_defaults(
        st.session_state["selected_model"],
        st.session_state["deployment_type"],
        st.session_state["foundry_mode"],
    )
    params = {k: str(st.session_state[k]) for k in _SHARE_STR_KEYS}
    if st.session_state["foundry_mode"]:
        params["foundry_mode"] = "1"
    for _k in _SHARE_FLOAT_KEYS:
        value = float(st.session_state.get(_k, defaults.get(_k, 0.0)))
        default = float(defaults.get(_k, value))
        if abs(value - default) > 1e-9:
            params[_k] = format(value, ".6g")
    st.query_params.clear()
    st.query_params.update(params)


if "_prev_controls" not in st.session_state:
    for _k, _v in compute_defaults(_INITIAL_MODEL, _INITIAL_DEPLOYMENT, False).items():
        st.session_state.setdefault(_k, _v)
    st.session_state.setdefault("selected_model", _INITIAL_MODEL)
    st.session_state.setdefault("deployment_type", _INITIAL_DEPLOYMENT)
    st.session_state.setdefault("region", available_regions(_INITIAL_MODEL, _INITIAL_DEPLOYMENT)[0])
    st.session_state.setdefault("foundry_mode", False)
    _hydrate_from_query_params()
    st.session_state["_prev_controls"] = (
        st.session_state["selected_model"],
        st.session_state["deployment_type"],
        st.session_state["foundry_mode"],
    )


def _reset_defaults():
    """Restore every assumption to its default for the current model/deployment/mode."""
    defaults = compute_defaults(
        st.session_state["selected_model"],
        st.session_state["deployment_type"],
        st.session_state["foundry_mode"],
    )
    for k, v in defaults.items():
        st.session_state[k] = v


def _dismiss_tour():
    """Hide the first-run guided tour."""
    st.session_state["_tour_dismissed"] = True


def _show_tour():
    """Bring the guided tour back (from the sidebar)."""
    st.session_state["_tour_dismissed"] = False


with st.sidebar:
    st.header("Quick actions")
    st.button("Reset to default assumptions", on_click=_reset_defaults)
    st.button("Show getting-started guide", on_click=_show_tour)
    st.markdown("**🔗 Shareable link**  \nThis page's URL updates as you edit. Copy it from the address bar to share or bookmark this exact scenario.")
    st.markdown("**Note**  \nThis tool provides **illustrative and directional guidance only** and is **not an official Azure PTU calculator**. Throughput assumptions, minimum PTU commitments, and pricing are subject to change. Always verify against current Azure documentation before making customer-specific decisions.")

# First-run guided tour — shown expanded on the first visit and dismissible. The
# sidebar "Show getting-started guide" button brings it back. Placed above the
# inputs so newcomers see the 3-step workflow before touching any control.
if not st.session_state.get("_tour_dismissed", False):
    with st.container(border=True):
        st.markdown("#### 👋 New here? Three steps to your answer")
        t1, t2, t3 = st.columns(3)
        with t1:
            st.markdown("**1 · Describe the workload**  \nPick a **model preset**, **deployment type**, and **region**, then enter **average RPM** and **tokens per request** on the left. Hover any ⓘ icon for an explanation.")
        with t2:
            st.markdown("**2 · Read the recommendation**  \nThe right panel gives **Recommended PTUs**, the **architecture pattern** (PTU-first / spillover / PAYGO), and the throughput behind it.")
        with t3:
            st.markdown("**3 · Compare cost & share**  \nThe **Monthly cost comparison** weighs PTU vs PAYGO vs spillover vs Priority. Use **📄 Export shareable report** to hand stakeholders a PDF.")
        st.markdown("**Two ways to feed this:** type estimates below, or import real usage via the KQL recipe (see README).")
        st.caption("Tip: start from a model preset — it fills throughput, minimum commit, and pricing for you. Toggle **Match Foundry calculator** to size for peak like the official tool. Guidance is directional; confirm final numbers in the official Azure PTU calculator before committing.")
        st.button("Got it — hide this", on_click=_dismiss_tour)

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
        avg_rpm = st.number_input(
            "Average RPM" if not foundry_mode else "Peak RPM",
            min_value=0.0,
            step=1.0,
            key="avg_rpm",
            help=("Sustained requests per minute at the workload's peak — size PTUs to this." if foundry_mode else "Sustained requests per minute on an average minute. Spikes are handled separately via the P95 multiplier, not by inflating this number."),
        )
        avg_input_tokens = st.number_input(
            "Average input tokens / request",
            min_value=0.0,
            step=1.0,
            key="avg_input_tokens",
            help="Mean prompt size per request (system + user + context). Drives input-token throughput and PAYGO input cost.",
        )
    with c2:
        avg_output_tokens = st.number_input(
            "Average output tokens / request",
            min_value=0.0,
            step=1.0,
            key="avg_output_tokens",
            help="Mean completion size per request. Output tokens are weighted more heavily for PTU sizing (see Output weighting) and usually cost more per token.",
        )
        p95_multiplier = st.slider(
            "P95 load multiplier",
            min_value=1.0,
            max_value=5.0,
            step=0.1,
            disabled=foundry_mode,
            key="p95_multiplier",
            help="How much higher the 95th-percentile minute runs versus the average minute. 2.0 means peak traffic is twice the average. Sets the peak reference PTUs and the burst ratio.",
        )
    with c3:
        cache_rate = st.slider(
            "Prompt cache rate",
            min_value=0.0,
            max_value=0.9,
            step=0.05,
            key="cache_rate",
            help="Fraction of input tokens served from the prompt cache. Cached tokens are billed at the cheaper cached rate, lowering effective input cost.",
        )
        baseline_load_factor = st.slider(
            "Baseline load factor",
            min_value=0.4,
            max_value=1.0,
            step=0.05,
            disabled=foundry_mode,
            key="baseline_load_factor",
            help="Share of average demand to provision as steady-state PTU baseline. Below 1.0 deliberately under-provisions and lets spikes spill to Standard — the field-guidance default.",
        )
        peak_minutes_fraction = st.slider("Peak minutes fraction", min_value=0.0, max_value=1.0, step=0.05, key="peak_minutes_fraction", help="Share of minutes the workload runs at its P95 peak (vs. its average minute). Drives how much traffic spills to Standard in the blended cost.")

    with st.expander("Advanced assumptions", expanded=True):
        a1, a2, a3, a4, a5 = st.columns(5)
        with a1:
            model_tpm_per_ptu = st.number_input("Model TPM per PTU", min_value=1.0, step=1.0, disabled=bool(preset), key="model_tpm_per_ptu", help="Tokens per minute one PTU delivers for this model (from the official PTU tables). Higher means fewer PTUs for the same throughput. Locked when a model preset is selected.")
        with a2:
            output_weight = st.number_input("Output weighting", min_value=0.0, step=0.1, disabled=bool(preset), key="output_weight", help="How many input-token-equivalents one output token costs for sizing. Output is more compute-intensive, so this is usually >1. Locked under a preset.")
        with a3:
            safety_buffer = st.number_input("Safety buffer", min_value=0.0, step=0.01, format="%.2f", disabled=foundry_mode, key="safety_buffer", help="Headroom added on top of the computed baseline PTUs (e.g. 0.15 = +15%) to absorb estimation error. Set to 0 in Foundry-match mode.")
        with a4:
            min_ptu_commit = st.number_input("Minimum PTU commit", min_value=0.0, step=1.0, disabled=bool(preset), key="min_ptu_commit", help="Smallest PTU count this deployment type can be provisioned at. The recommendation is rounded up to at least this. Locked under a preset.")
        with a5:
            ptu_scale_increment = st.number_input("PTU scale increment", min_value=1.0, step=1.0, disabled=bool(preset), key="ptu_scale_increment", help="Step size PTUs are sold in above the minimum (e.g. 5). The recommendation is rounded up to a multiple of this. Locked under a preset.")

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
            paygo_output_per_1m = st.number_input("PAYGO output / 1M tokens (USD)", min_value=0.0, step=0.01, key="paygo_output_per_1m", help="Standard pay-as-you-go output-token rate for this model and deployment type.")
        with b7:
            hours_per_month = st.number_input("Hours per month", min_value=1.0, step=1.0, key="hours_per_month", help="Hours used to convert hourly PTU pricing to a monthly figure (730 = a full month). Lower it to model part-time or business-hours-only running.")
        model_has_priority = model_supports_priority(preset)
        prio_help = (
            "Confirmed priority-tier rate for this model. Global base; Data Zone is 10% higher."
            if model_has_priority
            else "This model has no confirmed priority rate; the field is seeded from PAYGO x the fallback premium and the lane is marked not applicable."
        )
        b8, b9, b10 = st.columns(3)
        with b8:
            priority_input_per_1m = st.number_input("Priority input / 1M tokens (USD)", min_value=0.0, step=0.01, key="priority_input_per_1m", help=prio_help)
        with b9:
            priority_cached_per_1m = st.number_input("Priority cached input / 1M (USD)", min_value=0.0, step=0.01, key="priority_cached_per_1m", help="Cached prompt tokens billed at the priority tier's discounted rate.")
        with b10:
            priority_output_per_1m = st.number_input("Priority output / 1M tokens (USD)", min_value=0.0, step=0.01, key="priority_output_per_1m", help=prio_help)

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
    "priority_input_per_1m": priority_input_per_1m,
    "priority_cached_per_1m": priority_cached_per_1m,
    "priority_output_per_1m": priority_output_per_1m,
    "hours_per_month": hours_per_month,
    "spillover_supported": spillover_supported(deployment_type),
    "priority_supported": priority_supported(deployment_type) and model_supports_priority(preset),
}
calc = calculate(values)

# Surface boundary-condition problems (zero rate, zero tokens, zero pricing,
# implausibly large requests) so the numbers below are not silently misleading.
for _issue in validate_inputs(values, foundry_mode):
    (st.warning if _issue["level"] == "warning" else st.info)(_issue["message"])

# Keep the URL in sync with the current inputs so the page is shareable.
_sync_query_params()

with right:
    st.subheader("Outputs")
    k1, k2 = st.columns(2)
    k1.metric("Recommended PTUs", f'{calc["recommended_ptu"]:,.0f}', help="Baseline PTUs to provision: average demand x baseline load factor, plus safety buffer, rounded up to the deployment's minimum and scale increment. Size to this, not to peak.")
    k2.metric("Peak reference PTUs", f'{calc["peak_reference_ptu"]:,.0f}', help="PTUs the P95 peak alone would need (average x P95 multiplier). The gap above Recommended PTUs is what spills to Standard or Priority.")

    st.metric("Baseline TPM", f'{calc["baseline_tpm"]:,.0f}', help="Total tokens per minute at the provisioned baseline (input + output-weighted), net of prompt cache.")
    st.metric("P95 TPM", f'{calc["p95_tpm"]:,.0f}', help="Tokens per minute at the 95th-percentile peak minute — baseline TPM x P95 multiplier.")
    st.metric("Burst ratio (P95 / average)", f'{calc["burst_ratio"]:,.2f}x', help="Peak-to-average ratio. Higher = spikier traffic, which favors a smaller PTU baseline with spillover over provisioning for the peak.")

    st.markdown(f"### {calc['architecture']['badge']} {calc['architecture']['label']}")
    st.write(calc['architecture']['summary'])
    st.caption(calc['architecture']['reason'])
    st.info(calc['reservation_note'])

st.subheader("Monthly cost comparison")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("PTU monthly (1-mo reserved)", f'${calc["ptu_monthly"]:,.0f}', help=f'Hourly list: ${calc["ptu_hourly_monthly"]:,.0f}/mo before any reservation discount.')
m2.metric("PAYGO monthly", f'${calc["paygo_monthly"]:,.0f}', help='Pure pay-as-you-go: every token billed at Standard rates, no PTU commitment. The break-even reference for the other lanes.')
m3.metric("PTU + spillover", f'${calc["blended_monthly"]:,.0f}', delta=f'{calc["spill_fraction"]*100:,.1f}% on Standard', delta_color="off", help=f'Reserved PTU baseline plus PAYGO for the ~{calc["spill_fraction"]*100:,.1f}% of monthly demand that exceeds provisioned capacity, given the peak-minutes duty cycle.')
if calc["priority_supported"]:
    _prio_premium = (calc["priority_monthly"] / calc["paygo_monthly"] - 1) * 100 if calc["paygo_monthly"] else 0.0
    _prio_src = "confirmed per-model rates" if calc["priority_rate_source"] == "confirmed" else f'~{calc["priority_multiplier"]:.2f}x PAYGO fallback'
    m4.metric("Priority processing", f'${calc["priority_monthly"]:,.0f}', delta=f'+{_prio_premium:,.0f}% vs PAYGO', delta_color="off", help=f'Standard token volume billed at the priority tier ({_prio_src}) for a defined latency target with no PTU commitment.')
else:
    m4.metric("Priority processing", "n/a", help='Priority processing requires a supported model (gpt-5.x / gpt-4.1 family) on a Global or Data Zone (US) Standard deployment.')
delta_label = "PTU saves" if calc["savings_delta"] >= 0 else "PAYGO saves"
m5.metric(delta_label, f'${abs(calc["savings_delta"]):,.0f}', help='Monthly difference between the PTU (reserved) and PAYGO lanes. "PTU saves" means reserving is cheaper at this volume; "PAYGO saves" means usage is too low or too spiky to justify a reservation.')

st.caption(
    "**Priority processing** requires a model version of **2025-12-01 or later** and is offered only on **Global** and **Data Zone (US) Standard** deployments — Data Zone priority covers **US data zones only**. Confirm the live pricing page before quoting."
)

# Side-by-side view of the monthly cost lanes so the comparison is visual, not
# just the metric tiles above. Priority is only charted when it applies.
cost_rows = [
    {"Lane": "PTU (reserved)", "Monthly $": calc["ptu_monthly"]},
    {"Lane": "PAYGO", "Monthly $": calc["paygo_monthly"]},
    {"Lane": "PTU + spillover", "Monthly $": calc["blended_monthly"]},
]
if calc["priority_supported"]:
    cost_rows.append({"Lane": "Priority", "Monthly $": calc["priority_monthly"]})
cost_chart_df = pd.DataFrame(cost_rows)
cost_chart = (
    alt.Chart(cost_chart_df)
    .mark_bar()
    .encode(
        x=alt.X("Lane:N", sort=[r["Lane"] for r in cost_rows], title=None),
        y=alt.Y("Monthly $:Q", title="Monthly cost (USD)"),
        tooltip=["Lane", alt.Tooltip("Monthly $:Q", format=",.0f")],
    )
)
st.altair_chart(cost_chart, width="stretch")

# Read-only provenance panel: the exact rates and assumptions feeding the lanes
# above, so stakeholders can sanity-check a number without opening the export.
with st.expander("Pricing assumptions behind these numbers", expanded=False):
    _assump_rows = [
        ("PTU hourly price (USD)", f'${values["ptu_hourly_price"]:,.2f}/PTU/hr'),
        ("Monthly reservation discount", f'{values["reservation_discount_monthly"]*100:,.0f}%'),
        ("Yearly reservation discount", f'{values["reservation_discount_yearly"]*100:,.0f}%'),
        ("Hours per month", f'{values["hours_per_month"]:,.0f}'),
        ("PAYGO input / 1M tokens", f'${values["paygo_input_per_1m"]:,.2f}'),
        ("PAYGO cached input / 1M", f'${values["paygo_cached_per_1m"]:,.2f}'),
        ("PAYGO output / 1M tokens", f'${values["paygo_output_per_1m"]:,.2f}'),
        ("Model TPM per PTU", f'{values["model_tpm_per_ptu"]:,.0f}'),
        ("Output weighting", f'{values["output_weight"]:,.1f}x'),
    ]
    if calc["priority_supported"]:
        _assump_rows.extend([
            ("Priority input / 1M tokens", f'${values["priority_input_per_1m"]:,.2f}'),
            ("Priority output / 1M tokens", f'${values["priority_output_per_1m"]:,.2f}'),
        ])
    st.dataframe(
        pd.DataFrame(_assump_rows, columns=["Assumption", "Value"]),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"Pricing confirmed against the Azure OpenAI pricing page as of {PRICING_CONFIRMED_AS_OF} — "
        f"[{PRICING_SOURCE_URL}]({PRICING_SOURCE_URL}). Edit any value in the **Cost assumptions** panel on the left; "
        "these figures update live and flow into the exported report."
    )

# Break-even view: PTU is an architecture decision, not just a discount. Sweep
# the request rate and show where the reserved PTU baseline overtakes PAYGO, so
# the crossover (and where this workload sits relative to it) is visible.
_be_tier = st.radio(
    "PTU pricing tier for break-even",
    ["Hourly", "Monthly reservation", "Yearly reservation"],
    index=1,
    horizontal=True,
    help="Which provisioned commitment drives the PTU line. Cheaper tiers (yearly) lower the PTU slope and can move the crossover into range.",
)
_be_tier_label = {
    "Hourly": "PTU (hourly)",
    "Monthly reservation": "PTU (1-mo reservation)",
    "Yearly reservation": "PTU (1-yr reservation)",
}[_be_tier]
_be = breakeven_series(values, ptu_tier=_be_tier)
_be_verdict = None  # populated below; carried into the exported report
if _be["rows"]:
    _rpm_label = "Peak requests per minute" if foundry_mode else "Average requests per minute"
    _rpm_top = _be["rows"][-1]["rpm"]
    be_records = []
    for _r in _be["rows"]:
        be_records.append({"RPM": _r["rpm"], "Lane": _be_tier_label, "Monthly $": _r["ptu_monthly"]})
        be_records.append({"RPM": _r["rpm"], "Lane": "PAYGO", "Monthly $": _r["paygo_monthly"]})
    be_df = pd.DataFrame(be_records)

    # Recommended-choice callout: compare PTU (selected tier) vs PAYGO at the
    # current operating point so the chart resolves to an actual decision.
    _tier_ptu_now = next(
        (t["total_monthly"] for t in calc["pricing_tiers"] if t["term"] == _be_tier),
        calc["ptu_monthly"],
    )
    _paygo_now = calc["paygo_monthly"]
    _diff = _paygo_now - _tier_ptu_now  # > 0 means PTU is cheaper at current load
    if _diff > 0:
        st.success(
            f"**Recommended: provision PTU ({_be_tier}).** At your current load of "
            f"{_be['current_rpm']:,.0f} RPM it runs **~\\${_diff:,.0f}/mo cheaper** than PAYGO "
            f"(\\${_tier_ptu_now:,.0f} vs \\${_paygo_now:,.0f})."
        )
    else:
        st.info(
            f"**PTU ({_be_tier}) costs ~\\${-_diff:,.0f}/mo more than PAYGO** at your current load of "
            f"{_be['current_rpm']:,.0f} RPM (\\${_tier_ptu_now:,.0f} vs \\${_paygo_now:,.0f}). "
            f"**PTU may still be the right call above this cost premium** — for guaranteed throughput, "
            f"consistent low latency, and no PAYGO rate-limiting (429s)."
        )

    # Capture the decision so the exported report carries it, not just the inputs.
    _be_verdict = {
        "tier": _be_tier,
        "current_rpm": _be["current_rpm"],
        "ptu_now": _tier_ptu_now,
        "paygo_now": _paygo_now,
        "diff": _diff,  # > 0 means PTU is cheaper at current load
        "breakeven_rpm": _be["breakeven_rpm"],
        "breakeven_cost": (
            calculate({**values, "avg_rpm": _be["breakeven_rpm"]})["paygo_monthly"]
            if _be["breakeven_rpm"] else None
        ),
    }

    # Region shading: green where PAYGO is the cheaper architecture, blue where
    # the PTU baseline wins. Drawn first so the cost lines sit on top.
    be_layers = []
    if _be["breakeven_rpm"]:
        be_layers.append(
            alt.Chart(pd.DataFrame({"x0": [0.0], "x1": [_be["breakeven_rpm"]]}))
            .mark_rect(color="#2e7d32", opacity=0.07)
            .encode(x=alt.X("x0:Q", title=_rpm_label), x2="x1:Q", tooltip=alt.value(None))
        )
        be_layers.append(
            alt.Chart(pd.DataFrame({"x0": [_be["breakeven_rpm"]], "x1": [_rpm_top]}))
            .mark_rect(color="#0a6ed1", opacity=0.07)
            .encode(x=alt.X("x0:Q", axis=None), x2="x1:Q", tooltip=alt.value(None))
        )
    else:
        be_layers.append(
            alt.Chart(pd.DataFrame({"x0": [0.0], "x1": [_rpm_top]}))
            .mark_rect(color="#2e7d32", opacity=0.06)
            .encode(x=alt.X("x0:Q", title=_rpm_label), x2="x1:Q", tooltip=alt.value(None))
        )

    be_lines = (
        alt.Chart(be_df)
        .mark_line()
        .encode(
            x=alt.X("RPM:Q", title=_rpm_label),
            y=alt.Y("Monthly $:Q", title="Monthly cost (USD)"),
            color=alt.Color("Lane:N", title=None),
            tooltip=[alt.Tooltip("RPM:Q", format=",.0f"), "Lane", alt.Tooltip("Monthly $:Q", format=",.0f")],
        )
    )
    be_layers.append(be_lines)
    be_layers.append(
        alt.Chart(pd.DataFrame({"RPM": [_be["current_rpm"]]}))
        .mark_rule(strokeDash=[4, 4], color="#6b6b75")
        .encode(x="RPM:Q")
    )
    if _be["breakeven_rpm"]:
        be_layers.append(
            alt.Chart(pd.DataFrame({"RPM": [_be["breakeven_rpm"]]}))
            .mark_rule(color="#0a6ed1")
            .encode(x="RPM:Q")
        )
        # Crossover annotation: dot + label at the break-even cost so the exact
        # dollar figure is readable, not just the RPM.
        _be_cost = calculate({**values, "avg_rpm": _be["breakeven_rpm"]})["paygo_monthly"]
        _dot_df = pd.DataFrame({
            "RPM": [_be["breakeven_rpm"]],
            "Monthly $": [_be_cost],
            "label": [f"${_be_cost:,.0f}/mo @ {_be['breakeven_rpm']:,.0f} RPM"],
        })
        be_layers.append(
            alt.Chart(_dot_df).mark_point(size=90, filled=True, color="#0a6ed1").encode(x="RPM:Q", y="Monthly $:Q")
        )
        be_layers.append(
            alt.Chart(_dot_df)
            .mark_text(align="left", dx=8, dy=-8, color="#0a6ed1", fontWeight="bold")
            .encode(x="RPM:Q", y="Monthly $:Q", text="label:N")
        )
    be_chart = alt.layer(*be_layers).properties(height=360, padding={"left": 5, "top": 5, "right": 5, "bottom": 45})
    st.altair_chart(be_chart, width="stretch")
    if _be["breakeven_rpm"]:
        _be_side = "above" if _be["current_rpm"] >= _be["breakeven_rpm"] else "below"
        st.caption(
            f"Break-even ≈ **{_be['breakeven_rpm']:,.0f} RPM** (blue dot) at the **{_be_tier}** tier: the green band is where PAYGO is cheaper, the blue band where PTU wins. "
            f"Your current load of {_be['current_rpm']:,.0f} RPM (grey dashed) sits **{_be_side}** break-even."
        )
    else:
        _cheaper_hint = (
            "" if _be_tier == "Yearly reservation"
            else " — or try a longer reservation term to lower the PTU line"
        )
        st.caption(
            f"Across the charted range PAYGO stays cheaper on price than the **{_be_tier}** PTU baseline (green band) — this workload sits below the PTU cost break-even. "
            f"Current load: {_be['current_rpm']:,.0f} RPM (grey dashed). PTU can still be worth it for guaranteed throughput, steady latency, and no 429 throttling{_cheaper_hint}."
        )

# Sensitivity: estimates are only as good as their inputs, so show how the
# recommendation and the PTU-vs-PAYGO decision move when demand flexes ±20%.
with st.expander("Sensitivity — how the answer moves if your estimate is off (±20%)", expanded=False):
    _sens = sensitivity_table(values, ptu_tier=_be_tier)

    def _verdict_cell(row):
        d = row["diff"]
        return f"PTU −${d:,.0f}" if d > 0 else f"PAYGO −${-d:,.0f}"

    def _sens_df(rows):
        return pd.DataFrame([
            {
                "Change": r["label"],
                "Recommended PTUs": f'{r["recommended_ptu"]:,.0f}',
                "PTU $/mo": f'${r["ptu_monthly"]:,.0f}',
                "PAYGO $/mo": f'${r["paygo_monthly"]:,.0f}',
                "Cheaper lane": _verdict_cell(r),
                "Break-even RPM": (f'{r["breakeven_rpm"]:,.0f}' if r["breakeven_rpm"] else "—"),
            }
            for r in rows
        ])

    st.caption(
        f"PTU lane uses the **{_be_tier}** tier (matches the break-even chart above). "
        "“Cheaper lane” shows which option wins and by how much per month at each scenario; "
        "“Break-even RPM” is where PTU overtakes PAYGO (— means no crossover in range)."
    )
    _s1, _s2 = st.columns(2)
    with _s1:
        st.markdown("**If request rate (RPM) is off**")
        st.dataframe(_sens_df(_sens["rpm"]), hide_index=True, width="stretch")
    with _s2:
        st.markdown("**If tokens per request are off**")
        st.dataframe(_sens_df(_sens["tokens"]), hide_index=True, width="stretch")

# One-click shareable report — a self-contained HTML file stakeholders can open
# in any browser and "Save as PDF". Built from the same inputs/result as the page.
_report_meta = {
    "model": selected_model,
    "deployment_type": deployment_type,
    "region": region,
    "foundry_mode": foundry_mode,
    "breakeven": _be_verdict,
}
_report_html = build_report_html(values, calc, _report_meta)
_safe_stem = f'ptu-sizing-{str(selected_model).replace(" ", "_")}-{deployment_type.replace(" ", "_")}'
_report_name = f'{_safe_stem}.html'
_export_html_col, _export_csv_col = st.columns(2)
with _export_html_col:
    st.download_button(
        "📄 Export shareable report (HTML / print to PDF)",
        data=_report_html,
        file_name=_report_name,
        mime="text/html",
        width="stretch",
        help="Downloads a self-contained report (inputs, recommendation, all four cost lanes, assumptions). Open it in a browser and use Print → Save as PDF to share with stakeholders.",
    )
with _export_csv_col:
    st.download_button(
        "📊 Export cost lanes (CSV for Excel)",
        data=build_report_csv(values, calc, _report_meta),
        file_name=f'{_safe_stem}.csv',
        mime="text/csv",
        width="stretch",
        help="Downloads the four monthly cost lanes plus the inputs and assumptions as a flat Section/Item/Value CSV — drop it straight into Excel.",
    )

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
st.dataframe(pricing_df, width="stretch", hide_index=True)

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
st.altair_chart(ptu_chart, width="stretch")

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
st.dataframe(summary_df, width="stretch", hide_index=True)

st.markdown("---")
st.caption(
    "Designed for illustrative and directional sizing guidance. It reflects the same field guidance used in your PTU playbook: size PTU for the steady-state baseline, use Standard or PAYGO for spillover, and treat reservation as a billing optimization after validation."
)
