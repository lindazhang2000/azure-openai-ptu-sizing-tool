"""Record a narrated screen walkthrough of the live PTU sizing tool.

Drives https://ptu-sizing.com/ through the three demo scenarios from
docs/demo-script.md (Steady chatbot / Bursty RAG / Spiky low-baseline) and
records the whole session to a .webm video via Playwright.

Usage:
    pip install playwright
    playwright install chromium
    python scripts/record_demo.py [--url URL] [--out DIR] [--headed]
                                  [--mp4] [--gif] [--voice] [--voice-name NAME] [--rate N]

The video is written to docs/demo/ptu-sizing-demo.webm by default. Pass --mp4
and/or --gif to also produce those formats (requires ffmpeg on PATH). Pass
--voice to add a spoken narration track (Windows SAPI text-to-speech, offline)
and mux it into docs/demo/ptu-sizing-demo-narrated.mp4.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable

from playwright.sync_api import Page, sync_playwright

DEFAULT_URL = "https://ptu-sizing.com/"
DEFAULT_OUT = "docs/demo"
VIEWPORT = {"width": 1600, "height": 1000}

# Scenario matrix mirrors the table in docs/demo-script.md.
SCENARIOS = [
    {
        "name": "A · Steady chatbot",
        "verdict": "Expect: PTU-first — predictable traffic fills dedicated capacity.",
        "say": "First, a steady chatbot: thirty requests a minute with low burst. "
               "Because the traffic is predictable and fills capacity, the tool "
               "recommends a PTU-first production baseline.",
        "rpm": 30,
        "input_tokens": 1200,
        "output_tokens": 400,
        "p95": 1.5,
        "cache": 0.30,
    },
    {
        "name": "B · Bursty RAG",
        "verdict": "Expect: PTU + Standard spillover — size PTU to baseline, absorb spikes on Standard.",
        "say": "Next, a bursty RAG workload: higher volume with a two point eight times peak. "
               "The tool suggests sizing PTU to the baseline and letting a Standard "
               "deployment spill over to absorb the spikes.",
        "rpm": 120,
        "input_tokens": 2500,
        "output_tokens": 800,
        "p95": 2.8,
        "cache": 0.20,
    },
    {
        "name": "C · Spiky / low baseline",
        "verdict": "Expect: PAYGO — a committed PTU deployment would sit idle.",
        "say": "Finally, a spiky, low baseline app: very peaky at four point five times average. "
               "Here a committed PTU deployment would sit idle, so the tool recommends "
               "pay as you go.",
        "rpm": 15,
        "input_tokens": 900,
        "output_tokens": 300,
        "p95": 4.5,
        "cache": 0.10,
    },
]

INTRO_SAY = (
    "This is the Azure OpenAI PTU sizing tool at ptu dash sizing dot com. "
    "It turns a few workload numbers into an architecture recommendation: "
    "PTU, pay as you go, or spillover. Let's walk through three workloads."
)
OUTRO_SAY = (
    "Remember, this is directional guidance. Always confirm the final numbers "
    "in the official Azure PTU calculator before committing capacity."
)

# Narration timeline captured during the recording: [{"t": seconds, "text": str}].
TIMELINE: list[dict] = []
_T0 = 0.0


def mark(text: str) -> None:
    """Record a narration cue at the current offset from recording start."""
    TIMELINE.append({"t": round(time.monotonic() - _T0, 2), "text": text})


# Slider ranges from the live UI, used for deterministic position clicks.
SLIDER_RANGE = {
    "P95 load multiplier": (1.0, 5.0),
    "Prompt cache rate": (0.0, 0.9),
}


def caption(page: Page, title: str, body: str) -> None:
    """Show a fixed banner overlay so the recording is self-explanatory."""
    page.evaluate(
        """({title, body}) => {
            let el = document.getElementById('demo-caption');
            if (!el) {
                el = document.createElement('div');
                el.id = 'demo-caption';
                el.style.cssText = [
                    'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
                    'padding:14px 22px', 'font-family:Segoe UI, system-ui, sans-serif',
                    'background:linear-gradient(90deg,#0b3d91,#1668c1)', 'color:#fff',
                    'box-shadow:0 2px 10px rgba(0,0,0,.35)', 'pointer-events:none'
                ].join(';');
                document.body.appendChild(el);
            }
            el.innerHTML =
                '<div style="font-size:22px;font-weight:700">' + title + '</div>' +
                '<div style="font-size:15px;opacity:.92;margin-top:2px">' + body + '</div>';
        }""",
        {"title": title, "body": body},
    )


def set_number(page: Page, label: str, value: float) -> None:
    field = page.locator(f'input[aria-label="{label}"]').first
    field.scroll_into_view_if_needed()
    field.click()
    field.press("Control+a")
    field.fill(str(value))
    field.press("Enter")
    page.wait_for_timeout(600)


def set_slider(page: Page, label: str, target: float) -> None:
    lo, hi = SLIDER_RANGE[label]
    slider = page.locator(f'[role="slider"][aria-label="{label}"]').first
    slider.scroll_into_view_if_needed()
    box = slider.bounding_box()
    if not box:
        return
    frac = max(0.0, min(1.0, (target - lo) / (hi - lo)))
    page.mouse.click(box["x"] + frac * box["width"], box["y"] + box["height"] / 2)
    page.wait_for_timeout(800)


def dwell(page: Page, ms: int) -> None:
    page.wait_for_timeout(ms)


def convert(src: pathlib.Path, fmt: str) -> None:
    """Transcode the recorded .webm to mp4 or gif via ffmpeg, if available."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(f"Skipping {fmt}: ffmpeg not found on PATH.")
        return
    dest = src.with_suffix(f".{fmt}")
    if fmt == "mp4":
        cmd = [ffmpeg, "-y", "-i", str(src),
               "-movflags", "faststart", "-pix_fmt", "yuv420p",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(dest)]
    else:  # gif via palette for decent quality
        cmd = [ffmpeg, "-y", "-i", str(src),
               "-vf", "fps=12,scale=1000:-1:flags=lanczos,"
                      "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
               str(dest)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Saved {fmt} to {dest}")
    else:
        print(f"ffmpeg {fmt} conversion failed:\n{result.stderr[-500:]}")


_SYNTH_PS1 = r"""
param($Manifest, $Rate, $VoiceName)
Add-Type -AssemblyName System.Speech
$items = Get-Content -Raw -Encoding UTF8 $Manifest | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = [int]$Rate
if ($VoiceName) { try { $s.SelectVoice($VoiceName) } catch {} }
foreach ($it in $items) {
    $s.SetOutputToWaveFile($it.wav)
    $s.Speak($it.text)
}
$s.Dispose()
"""


def synth_windows(clips: list[dict], rate: int, voice_name: str | None) -> bool:
    """Synthesize each clip's text to its wav path using Windows SAPI. Returns success."""
    if sys.platform != "win32":
        print("Skipping narration: Windows SAPI text-to-speech is only available on Windows.")
        return False
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ptu-tts-"))
    ps1 = tmp / "synth.ps1"
    manifest = tmp / "manifest.json"
    ps1.write_text(_SYNTH_PS1, encoding="utf-8")
    manifest.write_text(json.dumps(clips), encoding="utf-8")
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
        "-Manifest", str(manifest), "-Rate", str(rate), "-VoiceName", voice_name or "",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Narration synthesis failed:\n{result.stderr[-500:]}")
        return False
    return all(pathlib.Path(c["wav"]).exists() for c in clips)


def load_env(path: str = ".env") -> None:
    """Populate os.environ from a .env file without overriding existing values."""
    env = pathlib.Path(path)
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def synth_azure(clips: list[dict]) -> bool:
    """Synthesize clips with Azure AI Speech neural TTS using .env credentials."""
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError:
        print("Skipping narration: run `pip install azure-cognitiveservices-speech`.")
        return False
    key = os.environ.get("AZURE_SPEECH_KEY", "").strip()
    region = os.environ.get("AZURE_SPEECH_REGION", "").strip()
    voice = os.environ.get("AZURE_SPEECH_VOICE", "en-US-AvaMultilingualNeural").strip()
    endpoint = os.environ.get("AZURE_SPEECH_ENDPOINT", "").strip()
    if not key or not (region or endpoint):
        print("Skipping narration: set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION (or _ENDPOINT) in .env.")
        return False
    if endpoint:
        cfg = speechsdk.SpeechConfig(subscription=key, endpoint=endpoint)
    else:
        cfg = speechsdk.SpeechConfig(subscription=key, region=region)
    cfg.speech_synthesis_voice_name = voice
    for clip in clips:
        audio = speechsdk.audio.AudioOutputConfig(filename=clip["wav"])
        synth = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=audio)
        result = synth.speak_text_async(clip["text"]).get()
        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            detail = getattr(getattr(result, "cancellation_details", None), "error_details", "")
            print(f"Azure TTS failed ({result.reason}). {detail}")
            return False
    return all(pathlib.Path(c["wav"]).exists() for c in clips)


def narrate(video: pathlib.Path, timeline: list[dict], out_dir: pathlib.Path,
            synth: Callable[[list[dict]], bool]) -> None:
    """Synthesize the timeline to speech and mux it onto the video as an MP4."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("Skipping narration: ffmpeg not found on PATH.")
        return
    if not timeline:
        print("Skipping narration: empty timeline.")
        return

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ptu-wav-"))
    clips = [
        {"wav": str(tmp / f"line{i}.wav"), "text": entry["text"], "t": entry["t"]}
        for i, entry in enumerate(timeline)
    ]
    if not synth(clips):
        return

    # Delay each spoken clip to its cue time, then mix onto one track.
    inputs: list[str] = ["-i", str(video)]
    filters: list[str] = []
    labels: list[str] = []
    for i, clip in enumerate(clips):
        inputs += ["-i", clip["wav"]]
        delay_ms = max(0, int(clip["t"] * 1000))
        filters.append(f"[{i + 1}:a]adelay={delay_ms}:all=1[a{i}]")
        labels.append(f"[a{i}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(clips)}:normalize=0[aout]")
    dest = out_dir / "ptu-sizing-demo-narrated.mp4"
    cmd = [
        ffmpeg, "-y", *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "faststart",
        "-c:a", "aac", "-b:a", "160k",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Saved narrated video to {dest}")
    else:
        print(f"ffmpeg narration mux failed:\n{result.stderr[-800:]}")


def run_demo(page: Page) -> None:
    caption(
        page,
        "Azure OpenAI PTU Sizing Tool — ptu-sizing.com",
        "Turn a few workload numbers into an architecture recommendation: PTU vs PAYGO vs spillover.",
    )
    mark(INTRO_SAY)
    page.wait_for_load_state("networkidle")
    dwell(page, 8000)

    # Dismiss the getting-started guide for a cleaner view.
    hide = page.get_by_role("button", name="Got it — hide this")
    if hide.count():
        hide.first.click()
        dwell(page, 1200)

    for scenario in SCENARIOS:
        caption(page, scenario["name"], scenario["verdict"])
        mark(scenario["say"])
        dwell(page, 1500)

        set_number(page, "Average RPM", scenario["rpm"])
        set_number(page, "Average input tokens / request", scenario["input_tokens"])
        set_number(page, "Average output tokens / request", scenario["output_tokens"])
        set_slider(page, "P95 load multiplier", scenario["p95"])
        set_slider(page, "Prompt cache rate", scenario["cache"])

        # Let Streamlit finish the rerun, then reveal the recommendation.
        dwell(page, 1500)
        page.get_by_role("heading", name="Outputs").first.scroll_into_view_if_needed()
        dwell(page, 3500)

        # Scroll through the monthly cost comparison.
        cost = page.get_by_role("heading", name="Monthly cost comparison")
        if cost.count():
            cost.first.scroll_into_view_if_needed()
            dwell(page, 3500)

        # Back to the top for the next scenario.
        page.mouse.wheel(0, -4000)
        dwell(page, 1200)

    caption(
        page,
        "Directional guidance — always validate before committing",
        "Confirm final numbers in the official Azure PTU calculator. Repo: github.com/lindazhang2000/azure-openai-ptu-sizing-tool",
    )
    mark(OUTRO_SAY)
    dwell(page, 8000)



def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--headed", action="store_true", help="Show the browser while recording.")
    parser.add_argument("--mp4", action="store_true", help="Also produce an .mp4 (needs ffmpeg).")
    parser.add_argument("--gif", action="store_true", help="Also produce a .gif (needs ffmpeg).")
    parser.add_argument("--voice", action="store_true",
                        help="Add a spoken narration track (Windows SAPI + ffmpeg).")
    parser.add_argument("--voice-azure", action="store_true",
                        help="Add narration using Azure AI Speech neural TTS (reads .env).")
    parser.add_argument("--voice-name", default=None,
                        help="SAPI voice name, e.g. 'Microsoft Zira Desktop'.")
    parser.add_argument("--rate", type=int, default=0,
                        help="SAPI speaking rate, -10 (slow) to 10 (fast). Default 0.")
    args = parser.parse_args(argv)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(out_dir),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        global _T0
        _T0 = time.monotonic()
        page.set_default_timeout(30_000)
        page.goto(args.url, wait_until="domcontentloaded")
        try:
            run_demo(page)
        finally:
            video = page.video
            context.close()
            browser.close()
            if video:
                src = pathlib.Path(video.path())
                dest = out_dir / "ptu-sizing-demo.webm"
                if dest.exists():
                    dest.unlink()
                src.rename(dest)
                print(f"Saved recording to {dest}")
                if args.mp4:
                    convert(dest, "mp4")
                if args.gif:
                    convert(dest, "gif")
                if args.voice or args.voice_azure:
                    (out_dir / "narration-timeline.json").write_text(
                        json.dumps(TIMELINE, indent=2), encoding="utf-8")
                    if args.voice_azure:
                        load_env()
                        narrate(dest, TIMELINE, out_dir, synth_azure)
                    else:
                        narrate(dest, TIMELINE, out_dir,
                                lambda clips: synth_windows(clips, args.rate, args.voice_name))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
