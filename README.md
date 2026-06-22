# Azure OpenAI PTU Enablement

Field and workshop enablement material for **Azure OpenAI Provisioned Throughput Units (PTU)** — playbooks, one-pagers, decks, worksheets, and a small interactive **PTU sizing demo** (Streamlit app + Jupyter notebook).

> **Disclaimer:** Everything here is **indicative workshop/demo material**, not the official PTU calculator. Replace model throughput, minimum PTU commit, and pricing assumptions with validated, customer-specific values before any external use.

## Repository layout

| Path | Contents |
| --- | --- |
| [app/](app) | Primary copy of the PTU sizing demo: Streamlit app, notebook, README, and requirements. |
| [ptumain/](ptumain) | Standalone artifacts package (kept as a separate working copy). |
| [linkedin/](linkedin) | LinkedIn carousel and "Top 10 PTU mistakes" content. |
| Root `*.docx` / `*.pdf` / `*.pptx` / `*.xlsx` | Playbooks, one-pagers, exec/workshop decks, readiness checklists, and the sizing worksheet. |

### Key documents (root)

- `Azure_OpenAI_PTU_Playbook_Linda.*` — full PTU playbook (docx / pdf / pptx)
- `Azure_OpenAI_PTU_OnePager_Linda.*` — executive one-pager (docx / pdf)
- `PTU_Workshop_Facilitator_Deck_Linda.pptx`, `PTU_Workshop_in_a_Box_Linda.*` — workshop materials
- `PTU_Exec_Slides_Linda.pptx`, `PTU_Exec_Slide3_TalkTrack_Linda.pptx` — executive slides
- `PTU_Readiness_Checklist*.pptx/.docx` — readiness checklists
- `PTU_Sizing_Worksheet_Linda.xlsx` — sizing worksheet
- `PTU_vs_PAYGO_Cost_Optimization_Guide*.pptx` — PTU vs PAYGO cost guidance
- `Top 10 PTU Mistakes*.docx` — common pitfalls

## Running the demo

From the [app/](app) folder:

### Streamlit app

```bash
pip install -r requirements_ptu_demo.txt
streamlit run ptu_streamlit_app.py
```

### Jupyter notebook

```bash
pip install -r requirements_ptu_demo.txt
jupyter notebook PTU_Sizing_Demo_Notebook.ipynb
```

## What the demo does

Given workload inputs (average RPM, input/output tokens per request, P95 multiplier, prompt cache rate, etc.) and cost assumptions, the demo:

1. Estimates a steady-state **baseline PTU** recommendation and a **peak reference PTU** figure.
2. Compares **PTU vs PAYGO** monthly cost.
3. Suggests an architecture pattern based on burstiness (PTU-first, PTU + Standard spillover, or PAYGO / smaller PTU pilot).

The guiding principle: size PTU for the steady-state baseline, use Standard/PAYGO for spillover, and treat reservation as a billing optimization **after** workload validation — not as the first step.

## Example scenarios to try

Type these into the **Workload inputs**; leave the Advanced and Cost assumptions at their defaults. The architecture recommendation is driven by the **P95 load multiplier** (burst ratio = P95 / average).

| Input | A — Steady chatbot | B — Bursty RAG | C — Spiky / low baseline |
| --- | --- | --- | --- |
| Average RPM | 30 | 120 | 15 |
| Avg input tokens / request | 1200 | 2500 | 900 |
| Avg output tokens / request | 400 | 800 | 300 |
| P95 load multiplier | 1.5 | 2.8 | 4.5 |
| Prompt cache rate | 0.30 | 0.20 | 0.10 |
| Baseline load factor | 0.70 | 0.70 | 0.70 |

What you should see:

- **A** → 🔵 *PTU-first production baseline* (burst 1.50x), ~30 recommended PTUs.
- **B** → 🟢 *PTU + Standard spillover* (burst 2.80x), several hundred PTUs, PTU far cheaper than PAYGO.
- **C** → 🟠 *PAYGO or smaller PTU pilot* (burst 4.50x), small PTU count near the 15 minimum.

To flip the recommendation, keep everything else fixed and move just the **P95 multiplier**: below 2 = PTU-first, 2–4 = spillover, 4+ = PAYGO. To test the minimum-commit floor, set **Average RPM** to `1` — recommended PTUs should clamp to 15.

## Understanding the inputs

### Workload inputs

- **Average RPM** — average requests per minute. Drives total volume for both throughput sizing and monthly cost.
- **Avg input tokens / request** — prompt size. Only the non-cached portion counts toward throughput and cost.
- **Avg output tokens / request** — completion size. Output is the expensive part: weighted heavily in the throughput proxy and priced higher in PAYGO.
- **P95 load multiplier** — how much higher your 95th-percentile minute is vs. the average minute. This **is** the burst ratio and decides the architecture recommendation: `<2` → PTU-first, `2–4` → PTU + spillover, `≥4` → PAYGO.
- **Prompt cache rate** — fraction of input tokens served from prompt cache. These are removed from the effective input load (`input × (1 − cache_rate)`). Higher cache = less load and lower cost.
- **Baseline load factor** — the share of the P95 peak you size your committed PTU baseline to cover (0.70 = size for 70% of peak, let spillover handle the rest). Lower = smaller, cheaper PTU commit leaning more on Standard/PAYGO.

The core throughput number ("input-equivalent TPM"):

```
avgTPM      = RPM × (input × (1 − cacheRate) + output × outputWeight)
p95TPM      = avgTPM × P95multiplier
baselineTPM = p95TPM × baselineLoadFactor
```

### Advanced assumptions

- **Model TPM per PTU** — throughput (tokens/min) one PTU delivers for the chosen model. Key conversion from TPM to PTUs (`baselineTPM / modelTpmPerPtu`). **Replace with the validated per-model value** — it is a placeholder.
- **Output weighting** — multiplier applied to output tokens in the throughput proxy (default 4×) because generating tokens costs more capacity than reading them. Raising it sizes output-heavy workloads larger.
- **Safety buffer** — headroom added on top of the raw PTU estimate (0.15 = +15%) before rounding up, so you are not sized exactly at the edge.
- **Minimum PTU commit** — smallest PTU quantity you would actually purchase (model/contract minimum). The recommendation is floored here, so tiny workloads still show 15, not 1.

Putting it together:

```
recommendedPTU = max( ceil( (baselineTPM / modelTpmPerPtu) × (1 + safetyBuffer) ), minPtuCommit )
```

### Cost assumptions (PTU vs PAYGO comparison)

- **PTU hourly price (USD)** — price per PTU per hour → `recommendedPTU × price × hoursPerMonth`.
- **PAYGO input / 1M tokens** and **PAYGO output / 1M tokens** — consumption pricing, applied to the effective monthly input/output tokens.
- **Hours per month** — billing window (730 ≈ a full month) used for both PTU cost and total request volume.

All prices and the per-PTU throughput are **indicative placeholders** — swap in validated Azure values before sharing externally.

## Official Microsoft Foundry PTU references

The demo's sizing formula mirrors the official **normalized TPM** method. Always validate against current Microsoft Learn guidance and the in-portal capacity calculator:

- [Determine PTU sizing for a workload](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing) — sizing formulas, per-model `Input TPM per PTU` and output-to-input ratios, minimums, and scale increments.
- [Provisioned throughput billing and cost management](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput-billing) — hourly vs. Azure Reservations, sizing and managing reservations.
- [Operate provisioned throughput deployments in production](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-get-started) — quota, utilization (leaky-bucket), 429 handling, benchmarking, scaling.
- [Manage traffic with spillover for provisioned deployments](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management) — auto-routing overflow to a standard deployment and spillover cost mechanics.
- [Plan and manage costs (Microsoft Foundry)](https://learn.microsoft.com/en-us/azure/foundry/concepts/manage-costs) — Cost Management, meters, budgets; note portal estimates exclude PTU and discounts.
- [Quickstart: Create a provisioned throughput deployment](https://learn.microsoft.com/en-us/azure/foundry/openai/provisioned-quickstart) — deploy, make an inference call, and view utilization.
