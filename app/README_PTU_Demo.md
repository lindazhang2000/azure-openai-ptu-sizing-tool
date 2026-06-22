# PTU Sizing Demo Standalone Artifacts

This package contains two standalone artifacts derived from the interactive PTU sizing workshop demo:

1. **Streamlit app** – `ptu_streamlit_app.py`
2. **Jupyter notebook** – `PTU_Sizing_Demo_Notebook.ipynb`

## Streamlit

```bash
pip install -r requirements_ptu_demo.txt
streamlit run ptu_streamlit_app.py
```

## Notebook

```bash
pip install -r requirements_ptu_demo.txt
jupyter notebook PTU_Sizing_Demo_Notebook.ipynb
```

## Using the demo

1. Pick a **Model preset** (`gpt-5.2`, `gpt-5.1`, `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4o`, `Llama-3.3-70B`) or **Custom**. The preset fills model throughput, output weighting, minimum PTU commit, and scale increment.
2. Pick a **Deployment type** — `Global`, `Data Zone`, or `Regional`. Global/Data Zone use the lower minimums (e.g. 15 PTUs, 5 increment); Regional uses larger model-specific minimums (e.g. 50/50 or 25/25). Only the types each model supports are listed. The type also sets the indicative **hourly $/PTU** (Global ~$1.00 < Data Zone ~$1.10 < Regional ~$2.00); reservation prices do not vary by type. **Automatic spillover** (preview) is available on Global and Data Zone only — a Regional deployment with a bursty profile is flagged for *manual overflow*.
3. Enter workload inputs (Average RPM, input/output tokens, P95 multiplier, cache rate, etc.) and read the recommended PTUs, the PTU-vs-PAYGO cost comparison, and the architecture suggestion.

In the notebook, the same choices are set via the `MODEL_PRESET` / `DEPLOYMENT_TYPE` variables (or the optional ipywidgets dropdowns). Both the app and notebook share the same logic in `ptu_core.py`, so they cannot drift.

For a full walkthrough of every input, the sizing formulas, example scenarios, and the official Microsoft Learn references, see the root [README.md](../README.md).

## Important note

This is an **indicative workshop/demo artifact**, not the official PTU calculator. Replace model throughput, minimum PTU quantity, and pricing assumptions with validated customer-specific values before external sharing.
