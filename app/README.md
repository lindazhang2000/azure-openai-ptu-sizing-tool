# PTU Sizing Tool

This package contains two front-ends over the shared `ptu_core` sizing engine:

1. **Streamlit app** – `ptu_streamlit_app.py`
2. **Jupyter notebook** – `PTU_Sizing_Notebook.ipynb`

## Streamlit

```bash
pip install -r requirements.txt
streamlit run ptu_streamlit_app.py
```

## Notebook

```bash
pip install -r requirements.txt
jupyter notebook PTU_Sizing_Notebook.ipynb
```

## Using the tool

1. Pick a **Model preset** (`gpt-5.2`, `gpt-5.1`, `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4o`, `Llama-3.3-70B`) or **Custom**. The preset fills model throughput, output weighting, minimum PTU commit, and scale increment.
2. Pick a **Deployment type** — `Global`, `Data Zone`, or `Regional`. Global/Data Zone use the lower minimums (e.g. 15 PTUs, 5 increment); Regional uses larger model-specific minimums (e.g. 50/50 or 25/25). Only the types each model supports are listed. The type also sets the **hourly $/PTU** (confirmed: Global $1.00 < Data Zone $1.10 < Regional $2.00) and the **PAYGO token rates** (Global Standard base; Data Zone/Regional Standard exactly 10% higher); reservation prices do not vary by type. **Automatic spillover** (preview) is available on Global and Data Zone only — a Regional deployment with a bursty profile is flagged for *manual overflow*.
3. Sanity-check the **Region (indicative)** dropdown — it lists regions where the chosen model + deployment type is plausibly offered. These are **indicative subsets**, not live capacity: Global routes broadly (~25 regions), Data Zone is **US/EU only** (~13, no APAC), and Regional is the most constrained and **varies per model** (~11 for `gpt-4.1`). It's display/validation only and does not change the math — always confirm against the live [region-availability table](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure-region-availability?pivots=provisioned).
4. Enter workload inputs (Average RPM, input/output tokens, P95 multiplier, cache rate, etc.) and read the recommended PTUs, the PTU-vs-PAYGO cost comparison, and the architecture suggestion.

In the notebook, the same choices are set via the `MODEL_PRESET` / `DEPLOYMENT_TYPE` variables (or the optional ipywidgets dropdowns). Both the app and notebook share the same logic in `ptu_core.py`, so they cannot drift.

For a full walkthrough of every input, the sizing formulas, example scenarios, and the official Microsoft Learn references, see the root [README.md](../README.md).

## Important note

This is an **internal sizing tool**, not the official Azure PTU calculator. Re-verify model throughput, minimum PTU quantity, and pricing against current Azure docs before quoting customer-specific numbers.
