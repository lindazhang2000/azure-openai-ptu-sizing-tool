# Demo script — Azure OpenAI PTU Sizing Tool

A ~5–7 minute walkthrough for a screen recording. It has three acts: the
**Streamlit app** (visual sizing), the **`token_usage.py --demo` CLI** (real-shaped
usage data, zero Azure exposure), and a short **wrap**. Everything here is
reproducible — the CLI demo uses built-in synthetic data, so numbers are stable
across takes and nothing live is shown on camera.

> Times are rough. Narration is in *italics*; commands are in code blocks. Run each
> command once, off-camera if you like, to warm caches before recording.

---

## Before you record (off-camera setup)

```powershell
# From the repo root, in the project venv.
& ".venv-1\Scripts\Activate.ps1"
cd app
pip install -r requirements.txt        # first time only
cd ..
```

Recording hygiene:

- Terminal font ~16–18pt; clear scrollback before each take (`Clear-Host`).
- Browser zoom ~110–125% for the Streamlit app.
- The `--demo` CLI exposes **no** subscription IDs or account names, so you can show
  it full-screen. If you later show **live** data (`az login` + no `--demo`), decide
  whether to redact subscription/account names first.
- Pre-type long commands into a scratch file and paste them, so you don't fat-finger.

---

## Act 0 — Hook (~30s)

*"Picking Azure OpenAI capacity is a guessing game. Too few PTUs and you get
throttled; too many and you burn budget on idle capacity. This tool turns a few
workload numbers into an architecture recommendation, and it reads your real token
usage to find the peak you actually need to size for."*

Show the top of the README (the decision triangle image is a nice visual).

---

## Act 1 — The Streamlit app: interactive sizing (~2 min)

```powershell
cd app
streamlit run ptu_streamlit_app.py
```

*"First, the sizing app. I give it a workload and it tells me PTU-vs-PAYGO and how
many PTUs."*

Walk through the three example scenarios (just change the inputs live — the
recommendation flips):

| Input | A — Steady chatbot | B — Bursty RAG | C — Spiky / low baseline |
| --- | --- | --- | --- |
| Average RPM | 30 | 120 | 15 |
| Avg input tokens / request | 1200 | 2500 | 900 |
| Avg output tokens / request | 400 | 800 | 300 |
| P95 load multiplier (burst ratio) | 1.5 | 2.8 | 4.5 |
| Prompt cache rate | 0.30 | 0.20 | 0.10 |
| Baseline load factor | 0.70 | 0.70 | 0.70 |

Talking points as you switch:

- **A (burst 1.5x)** → *"Predictable traffic, so PTU-first — dedicated capacity runs
  near full utilization with stable latency."*
- **B (burst 2.8x)** → *"Moderate burst, so PTU + Standard spillover — size PTUs to the
  baseline and let Standard absorb spikes."*
- **C (burst 4.5x)** → *"Spiky and low baseline, so PAYGO — a committed PTU deployment
  would sit idle."*

*"The key knob is the P95 multiplier — the burst ratio. Below 2 is PTU-first, 2 to 4
is spillover, 4-plus is PAYGO. And notice the PTU number is rounded up to what you can
actually deploy, with the model's minimum commit and a safety buffer baked in."*

Stop the app with `Ctrl+C` when done.

---

## Act 2 — Real-shaped usage data: `token_usage.py --demo` (~2.5 min)

*"Sizing is only as good as the peak you feed it. This script reads actual token
usage from Azure Monitor — per deployment and per model — and finds the busiest
window. I'll run it in demo mode so the numbers are synthetic and reproducible, but
the shape is exactly what you'd see against a live subscription."*

```powershell
cd ..
python scripts/token_usage.py --demo --days 14 --ptu-hint
```

Point at the output:

- **Token usage** table — totals per deployment and per model, two accounts.
- **Peak demand** — *"the busiest single hour, with a tokens-per-minute rate."*
- **`~ model peak: gpt-4.1`** — *"`gpt-4.1` runs across two deployments here, so it
  shows the **concurrent** peak — what the model needs at once, not just per
  deployment."*
- **`-> ~N PTU`** (from `--ptu-hint`) — *"a directional baseline PTU per peak, using
  the same sizing logic as the app."*

Now the granularity story — run the same thing at a finer interval:

```powershell
python scripts/token_usage.py --demo --days 14 --interval PT5M
```

*"Same data, 5-minute buckets instead of hourly. Watch the subscription peak: the
per-minute rate jumps — roughly 2,000 a minute hourly becomes over 3,000 a minute at
5 minutes — and it lands at the :20 burst. The hourly bucket averaged that spike
away. **Size against the fine-grained peak, or you'll under-provision for real
bursts.**"*

Optionally show the exports (good for "feed this into a dashboard"):

```powershell
python scripts/token_usage.py --demo --interval PT5M --csv demo-usage.csv --json demo-usage.json
```

*"And it writes CSV and JSON — one row per deployment with the peak and a suggested
PTU — so you can drop it into a spreadsheet or feed the peak straight back into the
sizing app."*

> The `--demo` numbers are deterministic from the time window, so re-running gives the
> same peaks. For a live demo, drop `--demo` and `az login` first (needs Monitoring
> Reader on the accounts).

---

## Act 3 — Wrap (~30s)

*"So: the app frames PTU-vs-PAYGO and the architecture pattern, and the usage script
grounds it in the peak you actually serve. Both are open source — formulas, presets,
and assumptions are all editable."*

Show:

- The repo URL: `github.com/lindazhang2000/azure-openai-ptu-sizing-tool`
- The **Disclaimer** in the README (*"directional guidance — always validate against
  Microsoft's official PTU calculator before committing capacity"*).

---

## Cheat sheet (paste targets)

```powershell
# App
cd app; streamlit run ptu_streamlit_app.py        # Ctrl+C to stop

# Usage CLI — synthetic, safe to show full screen
python scripts/token_usage.py --demo --days 14 --ptu-hint
python scripts/token_usage.py --demo --days 14 --interval PT5M
python scripts/token_usage.py --demo --interval PT5M --csv demo-usage.csv --json demo-usage.json

# Live version (optional, redact account names)
az login
python scripts/token_usage.py --ptu-hint
```

Clean up generated demo files afterward:

```powershell
Remove-Item demo-usage.csv, demo-usage.json -ErrorAction SilentlyContinue
```
