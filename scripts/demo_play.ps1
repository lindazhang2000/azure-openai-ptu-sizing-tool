<#
.SYNOPSIS
    Self-running, narrated demo of the Azure OpenAI PTU Sizing Tool for screen
    recordings. Plays the CLI acts (synthetic data, no Azure needed) with on-screen
    narration and paced command execution, and cues the Streamlit app act.

.DESCRIPTION
    Companion to docs/demo-script.md. By default it is presenter-driven: it prints
    each narration block and the command, then waits for you to press Enter before
    running it, so you control pacing while recording. Use -Auto for hands-free
    playback with fixed pauses.

    All CLI acts use the built-in synthetic data (--demo), so nothing live is shown
    and the numbers are reproducible across takes.

.PARAMETER Auto
    Play hands-free: use timed pauses instead of waiting for Enter.

.PARAMETER PauseSeconds
    Pause length (seconds) between steps in -Auto mode. Default 7.

.PARAMETER Days
    Look-back window passed to the demo commands. Default 14.

.PARAMETER LaunchApp
    Start the Streamlit app in a separate process at the app act.

.EXAMPLE
    pwsh scripts/demo_play.ps1
    Presenter-driven run (press Enter to advance each step).

.EXAMPLE
    pwsh scripts/demo_play.ps1 -Auto -PauseSeconds 8
    Hands-free run with 8-second pauses.
#>
[CmdletBinding()]
param(
    [switch]$Auto,
    [int]$PauseSeconds = 7,
    [int]$Days = 14,
    [switch]$LaunchApp
)

$ErrorActionPreference = 'Stop'

# Resolve repo root and the project venv python (fall back to PATH python).
$Root = Split-Path $PSScriptRoot -Parent
$Py = Join-Path $Root '.venv-1\Scripts\python.exe'
if (-not (Test-Path $Py)) { $Py = 'python' }

function Write-Banner([string]$Text) {
    Write-Host ''
    Write-Host ('=' * 92) -ForegroundColor DarkCyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ('=' * 92) -ForegroundColor DarkCyan
}

function Write-Narration([string]$Text) {
    Write-Host ''
    foreach ($line in ($Text -split "`n")) {
        Write-Host "  $line" -ForegroundColor Gray
    }
}

function Wait-Step {
    if ($Auto) {
        Start-Sleep -Seconds $PauseSeconds
    } else {
        Write-Host ''
        Write-Host '  [Enter] to continue...' -ForegroundColor DarkGray -NoNewline
        [void](Read-Host)
    }
}

# Echo a command like a live prompt, then run it.
function Invoke-Demo([string[]]$PyArgs) {
    $shown = 'python ' + ($PyArgs -join ' ')
    Write-Host ''
    Write-Host "  PS> " -ForegroundColor Green -NoNewline
    Write-Host $shown -ForegroundColor Yellow
    Write-Host ''
    & $Py @PyArgs
    Write-Host ''
}

Push-Location $Root
try {
    try { Clear-Host } catch { }
    Write-Banner 'Azure OpenAI PTU Sizing Tool - demo'
    Write-Narration @"
Picking Azure OpenAI capacity is a guessing game: too few PTUs and you get throttled,
too many and you pay for idle capacity. This tool turns a few workload numbers into an
architecture recommendation -- and it can read your REAL token usage to find the peak
you actually need to size for.

This walkthrough uses built-in synthetic data, so nothing live is shown and every
number is reproducible.
"@
    Wait-Step

    # ---- Act 1: the app -----------------------------------------------------
    Write-Banner 'Act 1 - Interactive sizing in the Streamlit app'
    Write-Narration @"
First, the app. Give it a workload and it answers: PTU or PAYGO, and how many PTUs.
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
minimum and a safety buffer baked in.
"@
    if ($LaunchApp) {
        Write-Host ''
        Write-Host '  Launching the Streamlit app in a separate process...' -ForegroundColor DarkGray
        Start-Process $Py -ArgumentList '-m','streamlit','run','app/ptu_streamlit_app.py' -WorkingDirectory $Root
    } else {
        Write-Narration 'Run in another terminal:  python -m streamlit run app/ptu_streamlit_app.py'
    }
    Wait-Step

    # ---- Act 2: real-shaped usage ------------------------------------------
    Write-Banner 'Act 2 - Real-shaped usage from Azure Monitor (token_usage.py)'
    Write-Narration @"
Sizing is only as good as the peak you feed it. This script reads actual token usage
from Azure Monitor -- per deployment and per model -- and finds the busiest window.
Running in demo mode so the numbers are synthetic but the shape is exactly what you'd
see live. Note the per-model CONCURRENT peak (gpt-4.1 spans two deployments) and the
~N PTU hint per peak.
"@
    Invoke-Demo @('scripts/token_usage.py','--demo','--days',"$Days",'--ptu-hint')
    Wait-Step

    Write-Narration @"
Now the granularity story: same data, 5-minute buckets instead of hourly. Watch the
SUBSCRIPTION PEAK -- the per-minute rate jumps and lands on the :20 burst. The hourly
bucket averaged that spike away. Size against the FINE-GRAINED peak, or you'll
under-provision for real bursts.
"@
    Invoke-Demo @('scripts/token_usage.py','--demo','--days',"$Days",'--interval','PT5M')
    Wait-Step

    # ---- Act 2.5: usage -> sizing inputs -----------------------------------
    Write-Banner 'Act 2.5 - Seed the sizing inputs from real usage (optional bridge)'
    Write-Narration @"
And you don't have to retype those numbers into the app. This optional bridge turns the
observed usage straight into the sizing tool's inputs -- average and peak throughput,
the burst ratio, and the matched model preset -- then runs the SAME sizing logic the app
uses. A real-traffic head start on the sizing decision.
"@
    Invoke-Demo @('scripts/usage_to_sizing.py','--demo','--days',"$Days",'--interval','PT5M','--calculate')
    Wait-Step

    # ---- Act 3: wrap --------------------------------------------------------
    Write-Banner 'Act 3 - Wrap'
    Write-Narration @"
So: the app frames PTU-vs-PAYGO and the architecture pattern, and the usage scripts
ground it in the peak you actually serve. All open source -- formulas, presets, and
assumptions are editable.

  Repo: github.com/lindazhang2000/azure-openai-ptu-sizing-tool

Directional guidance only -- always validate against Microsoft's official PTU
calculator and current pricing before committing capacity.
"@
    Write-Host ''
    Write-Host '  Demo complete.' -ForegroundColor Green
    Write-Host ''
}
finally {
    Pop-Location
}
