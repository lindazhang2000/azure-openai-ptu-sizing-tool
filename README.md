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

- **A** → 🔵 *PTU-first production baseline* (burst 1.50x), recommended PTUs rounded up to the model scale increment (multiples of 5 for OpenAI models).
- **B** → 🟢 *PTU + Standard spillover* (burst 2.80x), several hundred PTUs, PTU far cheaper than PAYGO.
- **C** → 🟠 *PAYGO or smaller PTU pilot* (burst 4.50x), small PTU count near the minimum.

To flip the recommendation, keep everything else fixed and move just the **P95 multiplier**: below 2 = PTU-first, 2–4 = spillover, 4+ = PAYGO. The recommendation also turns to *PAYGO / pilot* whenever the steady baseline needs fewer PTUs than the model minimum (e.g. set **Average RPM** to `1`), since a dedicated PTU deployment would sit idle.

## Understanding the inputs

### Model preset

- **Model preset** — selecting a model (`gpt-5.2`, `gpt-5.1`, `gpt-5`, `gpt-5-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano`, `gpt-4o`, `Llama-3.3-70B`) auto-fills the official sizing constants — **Model TPM per PTU**, **Output weighting** (output-to-input ratio), **Minimum PTU commit**, and **PTU scale increment** — and locks those fields. Choose **Custom** to edit them freely. Values mirror the Microsoft Learn sizing tables and should still be re-verified against current docs.
- **Deployment type** — `Global`, `Data Zone`, or `Regional` provisioned. Global and Data Zone share the lower minimum (15 PTUs for OpenAI models) and a 5-PTU scale increment; Regional uses larger model-specific minimums (e.g. 50 PTUs / 50 increment for `gpt-4.1`, 25 / 25 for the mini/nano models). The dropdown only lists the deployment types the selected model actually supports (e.g. `gpt-5.2` and `Llama-3.3-70B` are Global-only, `gpt-5.1` is Global + Data Zone); regional **availability also varies by region**, so confirm against the references below.
- **Match Foundry calculator (size for peak, no buffer)** — a checkbox that mirrors the official in-portal [PTU calculator](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing): RPM is treated as the **peak**, with `p95_multiplier = 1`, `baseline_load_factor = 1`, and `safety_buffer = 0`. With `gpt-5.1`, Peak RPM 200, 2000 input / 400 output tokens, and 50% cache it reproduces the calculator's **180 PTUs** exactly. Leave it unchecked for the field-guidance baseline + spillover view.

### Workload inputs

- **Average RPM** — average requests per minute. Drives total volume for both throughput sizing and monthly cost.
- **Avg input tokens / request** — prompt size. Only the non-cached portion counts toward throughput and cost.
- **Avg output tokens / request** — completion size. Output is the expensive part: weighted heavily in the throughput proxy and priced higher in PAYGO.
- **P95 load multiplier** — how much higher your 95th-percentile minute is vs. the average minute. This **is** the burst ratio. Combined with baseline scale it decides the architecture recommendation: `<2` → PTU-first, `2–4` → PTU + spillover, `≥4` → PAYGO — and any baseline below the model minimum is steered to PAYGO/pilot regardless.
- **Prompt cache rate** — fraction of input tokens served from prompt cache. These are removed from the effective input load (`input × (1 − cache_rate)`). Higher cache = less load and lower cost.
- **Baseline load factor** — the share of the P95 peak you size your committed PTU baseline to cover (0.70 = size for 70% of peak, let spillover handle the rest). Lower = smaller, cheaper PTU commit leaning more on Standard/PAYGO.
- **Peak minutes fraction** — share of minutes the workload actually runs at its P95 peak (vs. its average minute). Drives the blended spillover cost: spill is only paid for during the time demand exceeds provisioned capacity, so a low duty cycle (e.g. 10%) produces far less spillover than assuming the peak is constant.

The core throughput number ("input-equivalent TPM"):

```
avgTPM      = RPM × (input × (1 − cacheRate) + output × outputWeight)
p95TPM      = avgTPM × P95multiplier
baselineTPM = p95TPM × baselineLoadFactor
```

### Advanced assumptions

- **Model TPM per PTU** — throughput (tokens/min) one PTU delivers for the chosen model. Key conversion from TPM to PTUs (`baselineTPM / modelTpmPerPtu`). Set automatically by the model preset; placeholder when Custom.
- **Output weighting** — the model's output-to-input ratio applied to output tokens in the throughput proxy (4× for gpt-4.1, 8× for gpt-5) because generating tokens costs more capacity than reading them.
- **Safety buffer** — headroom added on top of the raw PTU estimate (0.15 = +15%) before rounding up, so you are not sized exactly at the edge. (The official method has no buffer — this is intentionally conservative.)
- **Minimum PTU commit** — smallest PTU quantity the model allows for the selected **Deployment type** (Global/Data Zone: 15 for OpenAI, 100 for Llama; Regional: 25–50 depending on the model). The recommendation is floored here.
- **PTU scale increment** — deployments can only be sized in fixed steps (Global/Data Zone: 5 for OpenAI, 100 for Llama; Regional: 25 or 50 depending on the model). The recommendation is rounded **up** to the next valid increment, matching what you can actually provision.

Putting it together:

```
roundedUp(x, inc) = ceil(x / inc) × inc
recommendedPTU    = max( roundedUp( (baselineTPM / modelTpmPerPtu) × (1 + safetyBuffer), increment ),
                         roundedUp( minPtuCommit, increment ) )
```

### Cost assumptions (PTU vs PAYGO comparison)

- **PTU hourly price (USD)** — list price per PTU per hour. This now **varies by Deployment type**: Microsoft introduced differentiated hourly pricing where Global is the lowest, Data Zone slightly higher, and Regional the highest. The field defaults from the selected deployment type (indicative: Global ~$1.00, Data Zone ~$1.10, Regional ~$2.00 per PTU/hr) and stays editable; re-verify per model and region on the Azure pricing page.
- **Monthly / Yearly reservation discount** — fraction off the hourly price for a 1-month or 1-year Azure Reservation. Defaults (~64% / ~70%) match the Foundry calculator's gpt-5.x tiers. The headline **PTU monthly** uses the 1-month reserved price. Note that **reservation prices do not vary by deployment type** — only the hourly rate differs.
- **PAYGO input / 1M tokens** and **PAYGO output / 1M tokens** — consumption pricing for uncached input and output tokens.
- **PAYGO cached input / 1M tokens** — cached prompt tokens are billed at a **discounted rate, not free**, so the comparison does not overstate PAYGO savings.
- **Hours per month** — billing window (730 ≈ a full month) used for both PTU cost and total request volume.

The app shows the same three-tier pricing table as the Foundry calculator (Hourly / Monthly reservation / Yearly reservation, with per-PTU cost and savings %), plus the PAYGO and blended spillover comparison:

```
PTU hourly       = recommendedPTU × hourlyPrice × hours
PTU 1-mo reserved = PTU hourly × (1 − monthlyDiscount)        # headline
PTU 1-yr reserved = PTU hourly × (1 − yearlyDiscount)
PAYGO monthly    = uncachedInput×inputRate + cachedInput×cachedRate + output×outputRate
PTU + spillover  = PTU 1-mo reserved + spillFraction × PAYGO monthly
```

where `spillFraction` is the time-weighted share of monthly demand above the provisioned PTU capacity. A simple duty cycle is used: for `peakMinutesFraction` of the time demand sits at the P95 level and at the average level the rest of the time, and spill is only counted where demand exceeds capacity in each regime:

```
capacity     = recommendedPTU × modelTpmPerPtu
spillDemand  = f × max(p95TPM − capacity, 0) + (1 − f) × max(avgTPM − capacity, 0)
totalDemand  = f × p95TPM + (1 − f) × avgTPM
spillFraction = spillDemand / totalDemand            # f = peakMinutesFraction
```

All prices and the per-PTU throughput are **indicative placeholders** — swap in validated Azure values before sharing externally.

## Official Microsoft Foundry PTU references

The demo's sizing formula mirrors the official **normalized TPM** method. Always validate against current Microsoft Learn guidance and the in-portal capacity calculator:

- [Determine PTU sizing for a workload](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-throughput-sizing) — sizing formulas, per-model `Input TPM per PTU` and output-to-input ratios, minimums, and scale increments.
- [Provisioned throughput billing and cost management](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput-billing) — hourly vs. Azure Reservations, sizing and managing reservations.
- [Operate provisioned throughput deployments in production](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/provisioned-get-started) — quota, utilization (leaky-bucket), 429 handling, benchmarking, scaling.
- [Manage traffic with spillover for provisioned deployments](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/spillover-traffic-management) — auto-routing overflow to a standard deployment and spillover cost mechanics.
- [Plan and manage costs (Microsoft Foundry)](https://learn.microsoft.com/en-us/azure/foundry/concepts/manage-costs) — Cost Management, meters, budgets; note portal estimates exclude PTU and discounts.
- [Quickstart: Create a provisioned throughput deployment](https://learn.microsoft.com/en-us/azure/foundry/openai/provisioned-quickstart) — deploy, make an inference call, and view utilization.
