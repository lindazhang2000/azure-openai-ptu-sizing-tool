#!/usr/bin/env bash
# Self-running, narrated demo of the Azure OpenAI PTU Sizing Tool (macOS/Linux).
# Companion to docs/demo-script.md and the PowerShell scripts/demo_play.ps1.
#
# All CLI acts use the built-in synthetic data (--demo), so nothing live is shown
# and the numbers are reproducible across takes.
#
# Usage:
#   scripts/demo_play.sh                 # presenter-driven (press Enter to advance)
#   scripts/demo_play.sh --auto          # hands-free with timed pauses
#   scripts/demo_play.sh --auto --pause 8
#   scripts/demo_play.sh --short --auto  # ~60-second teaser
#   scripts/demo_play.sh --launch-app    # also start the Streamlit app at Act 1
#
# Options:
#   --auto            Play hands-free (timed pauses instead of waiting for Enter).
#   --pause N         Pause seconds between steps in --auto mode (default 7).
#   --days N          Look-back window for the demo commands (default 14).
#   --short           60-second teaser: skip the app act and the hourly pass.
#   --launch-app      Start the Streamlit app in the background at Act 1.

set -euo pipefail

AUTO=0
PAUSE=7
DAYS=14
SHORT=0
LAUNCH_APP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto) AUTO=1; shift ;;
    --pause) PAUSE="$2"; shift 2 ;;
    --days) DAYS="$2"; shift 2 ;;
    --short) SHORT=1; shift ;;
    --launch-app) LAUNCH_APP=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# Resolve repo root and the project venv python (fall back to PATH python).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PY="$ROOT/.venv-1/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || command -v python)"

# Colors (disabled if not a TTY).
if [[ -t 1 ]]; then
  C_BANNER='\033[36m'; C_RULE='\033[2;36m'; C_NARR='\033[37m'
  C_PROMPT='\033[32m'; C_CMD='\033[33m'; C_DIM='\033[2m'; C_OK='\033[32m'; C_OFF='\033[0m'
else
  C_BANNER=''; C_RULE=''; C_NARR=''; C_PROMPT=''; C_CMD=''; C_DIM=''; C_OK=''; C_OFF=''
fi
RULE="$(printf '=%.0s' {1..92})"

banner() { printf '\n%b%s%b\n%b  %s%b\n%b%s%b\n' "$C_RULE" "$RULE" "$C_OFF" "$C_BANNER" "$1" "$C_OFF" "$C_RULE" "$RULE" "$C_OFF"; }
narrate() { printf '\n'; while IFS= read -r line; do printf '%b  %s%b\n' "$C_NARR" "$line" "$C_OFF"; done <<< "$1"; }
wait_step() {
  if [[ "$AUTO" -eq 1 ]]; then sleep "$PAUSE"; else printf '\n%b  [Enter] to continue...%b' "$C_DIM" "$C_OFF"; read -r _; fi
}
run_demo() {  # echo a live-looking prompt, then run
  printf '\n%b  $ %b%bpython %s%b\n\n' "$C_PROMPT" "$C_OFF" "$C_CMD" "$*" "$C_OFF"
  "$PY" "$@"
  printf '\n'
}

cd "$ROOT"

clear 2>/dev/null || true
banner 'Azure OpenAI PTU Sizing Tool - demo'
if [[ "$SHORT" -eq 1 ]]; then
  narrate "Sizing Azure OpenAI capacity is a guessing game -- too few PTUs and you throttle, too
many and you pay for idle. This tool reads your REAL token usage, finds the peak you
must size for, and turns it straight into a PTU recommendation. 60-second tour:"
else
  narrate "Picking Azure OpenAI capacity is a guessing game: too few PTUs and you get throttled,
too many and you pay for idle capacity. This tool turns a few workload numbers into an
architecture recommendation -- and it can read your REAL token usage to find the peak
you actually need to size for.

This walkthrough uses built-in synthetic data, so nothing live is shown and every
number is reproducible."
fi
wait_step

# ---- Act 1: the app (full only) -------------------------------------------
if [[ "$SHORT" -eq 0 ]]; then
  banner 'Act 1 - Interactive sizing in the Streamlit app'
  narrate "First, the app. Give it a workload and it answers: PTU or PAYGO, and how many PTUs.
Try these three scenarios live -- the recommendation flips with the burst ratio:

  Input                       A: Steady    B: Bursty RAG   C: Spiky/low
  Average RPM                 30           120             15
  Avg input tokens/req        1200         2500            900
  Avg output tokens/req       400          800             300
  P95 multiplier (burst)      1.5          2.8             4.5
  Prompt cache rate           0.30         0.20            0.10

  A (burst 1.5x) -> PTU-first        B (2.8x) -> PTU + spillover    C (4.5x) -> PAYGO

Key knob: the P95 multiplier (burst ratio). Below 2 = PTU-first, 2-4 = spillover,
4+ = PAYGO. The PTU number is rounded up to a deployable amount, with the model
minimum and a safety buffer baked in."
  if [[ "$LAUNCH_APP" -eq 1 ]]; then
    printf '%b  Launching the Streamlit app in the background...%b\n' "$C_DIM" "$C_OFF"
    ( "$PY" -m streamlit run app/ptu_streamlit_app.py >/dev/null 2>&1 & )
  else
    narrate "Run in another terminal:  python -m streamlit run app/ptu_streamlit_app.py"
  fi
  wait_step
fi

# ---- Act 2: real-shaped usage ---------------------------------------------
banner 'Act 2 - Real-shaped usage from Azure Monitor (token_usage.py)'
if [[ "$SHORT" -eq 1 ]]; then
  narrate "This reads actual token usage from Azure Monitor -- per deployment and per model -- and
finds the busiest window. Demo data here, but the shape is what you'd see live. Note the
per-model CONCURRENT peak (gpt-4.1 spans two deployments), the fine-grained tokens/min,
and the ~N PTU hint per peak."
  run_demo scripts/token_usage.py --demo --days "$DAYS" --interval PT5M --ptu-hint
  wait_step
else
  narrate "Sizing is only as good as the peak you feed it. This script reads actual token usage
from Azure Monitor -- per deployment and per model -- and finds the busiest window.
Running in demo mode so the numbers are synthetic but the shape is exactly what you'd
see live. Note the per-model CONCURRENT peak (gpt-4.1 spans two deployments) and the
~N PTU hint per peak."
  run_demo scripts/token_usage.py --demo --days "$DAYS" --ptu-hint
  wait_step

  narrate "Now the granularity story: same data, 5-minute buckets instead of hourly. Watch the
SUBSCRIPTION PEAK -- the per-minute rate jumps and lands on the :20 burst. The hourly
bucket averaged that spike away. Size against the FINE-GRAINED peak, or you'll
under-provision for real bursts."
  run_demo scripts/token_usage.py --demo --days "$DAYS" --interval PT5M
  wait_step
fi

# ---- Act 2.5: usage -> sizing inputs --------------------------------------
banner 'Act 2.5 - Seed the sizing inputs from real usage (optional bridge)'
narrate "And you don't have to retype those numbers into the app. This optional bridge turns the
observed usage straight into the sizing tool's inputs -- average and peak throughput,
the burst ratio, and the matched model preset -- then runs the SAME sizing logic the app
uses. A real-traffic head start on the sizing decision."
run_demo scripts/usage_to_sizing.py --demo --days "$DAYS" --interval PT5M --calculate
wait_step

# ---- Act 3: wrap ----------------------------------------------------------
banner 'Act 3 - Wrap'
if [[ "$SHORT" -eq 1 ]]; then
  narrate "Real usage in, a PTU recommendation out. Open source and directional -- validate against
Microsoft's official PTU calculator before committing capacity.
  Repo: github.com/lindazhang2000/azure-openai-ptu-sizing-tool"
else
  narrate "So: the app frames PTU-vs-PAYGO and the architecture pattern, and the usage scripts
ground it in the peak you actually serve. All open source -- formulas, presets, and
assumptions are editable.

  Repo: github.com/lindazhang2000/azure-openai-ptu-sizing-tool

Directional guidance only -- always validate against Microsoft's official PTU
calculator and current pricing before committing capacity."
fi
printf '\n%b  Demo complete.%b\n\n' "$C_OK" "$C_OFF"
