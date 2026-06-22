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
