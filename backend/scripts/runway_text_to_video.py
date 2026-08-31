#!/usr/bin/env python3
"""
Generate a video from a text prompt on Runway. Standalone — nothing to do with
the Memories in Stone pipeline, which runs on fal.ai.

This exists to spend leftover Runway credits on ad-hoc clips (marketing
snippets, tests, filler). Text-to-video only: the prompt file is the entire
input, no image is uploaded.

Usage:
    export RUNWAY_AI_KEY=...                  # or put it in ~/.bashrc
    python3 scripts/runway_text_to_video.py my_prompt.txt

    # portrait 720x1280 is the DEFAULT (social/marketing is vertical-first)
    python3 scripts/runway_text_to_video.py my_prompt.txt --landscape
    python3 scripts/runway_text_to_video.py my_prompt.txt --duration 10
    python3 scripts/runway_text_to_video.py my_prompt.txt --ratio 1080:1920
    python3 scripts/runway_text_to_video.py my_prompt.txt --model seedance2_fast

The prompt file is plain text; the whole file is the prompt. Blank lines and
surrounding whitespace are stripped, and lines beginning with # are dropped so
you can keep notes in the file.

Output lands next to the prompt file as <name>-<timestamp>.mp4 unless you pass
--out.

⚠️  Runway's seedance route blocks photographs of real people
    (INPUT_PREPROCESSING.SAFETY.THIRD_PARTY). That is an *input image* filter
    and does not apply to text-to-video, but generated output is still
    moderated — a prompt describing a real, named person may be refused.
    Blocked tasks cost 0 credits.

Stdlib only, no pip installs. Runs on system python3 (3.9+).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://api.dev.runwayml.com/v1"
API_VERSION = "2024-11-06"

DEFAULT_MODEL = "seedance2"
# Portrait by default: these clips are for social and marketing, which is
# vertical-first. --landscape switches, --ratio overrides both outright.
PORTRAIT_RATIO = "720:1280"         # 720p vertical
LANDSCAPE_RATIO = "1280:720"        # 720p horizontal
DEFAULT_DURATION = 5                # seconds; Runway caps at 15

# From the API's own validation error — the authoritative list, and cheaper to
# read here than to rediscover by submitting a bad request.
VALID_RATIOS = [
    "992:432", "864:496", "752:560", "640:640", "560:752", "496:864",
    "1470:630", "1280:720", "1112:834", "960:960", "834:1112", "720:1280",
    "2206:946", "1920:1080", "1664:1248", "1440:1440", "1248:1664",
    "1080:1920", "3840:1646", "3840:2160", "3840:2880", "3840:3840",
    "2880:3840", "2160:3840",
]
VALID_MODELS = [
    "gen4.5", "kling2.5_turbo_pro", "kling3.0_pro", "kling3.0_4k",
    "kling3.0_standard", "klingO3_pro", "klingO3_standard", "klingO3_4k",
    "seedance2", "seedance2_fast", "seedance2_mini", "seedance2_5",
    "hailuo3", "happyhorse_1_0", "grok_imagine_1_5", "veo3.1", "veo3.1_fast",
    "gemini_omni_flash", "gemini_omni_flash_1.1", "wan3",
]

POLL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 15 * 60


def _key() -> str:
    key = os.environ.get("RUNWAY_AI_KEY") or os.environ.get("RUNWAY_API_KEY", "")
    if not key:
        sys.exit(
            "RUNWAY_AI_KEY is not set.\n"
            "  export RUNWAY_AI_KEY=...   (or add it to ~/.bashrc and start a login shell)"
        )
    return key


def _request(url: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "X-Runway-Version": API_VERSION,
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:900]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def read_prompt(path: Path) -> str:
    """Whole file is the prompt. `#` lines are notes and are dropped."""
    if not path.is_file():
        sys.exit(f"No such prompt file: {path}")
    lines = [
        ln.rstrip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]
    prompt = " ".join(ln.strip() for ln in lines if ln.strip())
    if not prompt:
        sys.exit(f"{path} has no prompt text (only blank or # lines).")
    return prompt


def submit(key: str, prompt: str, model: str, ratio: str, duration: int) -> dict:
    return _request(f"{API}/text_to_video", key, {
        "model": model,
        "promptText": prompt,
        "ratio": ratio,
        "duration": duration,
    })


def wait_for(key: str, task_id: str) -> dict:
    """Poll to a terminal state. Returns the final task payload."""
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    last = ""
    while time.time() < deadline:
        task = _request(f"{API}/tasks/{task_id}", key)
        status = task.get("status", "?")
        if status != last:
            print(f"   {status}", flush=True)
            last = status
        if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return task
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"Timed out after {POLL_TIMEOUT_SECONDS // 60} minutes")


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a Runway video from a text prompt file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("prompt_file", type=Path, help="text file containing the prompt")
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=VALID_MODELS)
    orient = ap.add_mutually_exclusive_group()
    orient.add_argument("--portrait", action="store_true",
                        help=f"vertical {PORTRAIT_RATIO} — the default")
    orient.add_argument("--landscape", action="store_true",
                        help=f"horizontal {LANDSCAPE_RATIO}")
    ap.add_argument("--ratio", choices=VALID_RATIOS, metavar="WxH",
                    help="exact ratio, overrides --portrait/--landscape")
    ap.add_argument("--duration", type=int, default=DEFAULT_DURATION,
                    help=f"seconds, 1-15 (default {DEFAULT_DURATION})")
    ap.add_argument("--out", type=Path, help="output .mp4 path")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt and payload, submit nothing, spend nothing")
    args = ap.parse_args()

    if not 1 <= args.duration <= 15:
        sys.exit("--duration must be between 1 and 15 seconds")

    # --ratio wins if given; otherwise --landscape flips the portrait default.
    ratio = args.ratio or (LANDSCAPE_RATIO if args.landscape else PORTRAIT_RATIO)
    orientation = ("landscape" if ratio == LANDSCAPE_RATIO
                   else "portrait" if ratio == PORTRAIT_RATIO else "custom")

    prompt = read_prompt(args.prompt_file)
    print(f"prompt file : {args.prompt_file}")
    print(f"model       : {args.model}   {orientation} {ratio}   {args.duration}s")
    print(f"prompt      : {prompt}\n")

    if args.dry_run:
        print("--dry-run: nothing submitted, no credits spent.")
        return

    key = _key()
    try:
        task = submit(key, prompt, args.model, ratio, args.duration)
    except RuntimeError as e:
        sys.exit(f"Submit rejected: {e}")

    task_id = task.get("id")
    if not task_id:
        sys.exit(f"No task id in response: {task}")
    est = (task.get("estimatedCost") or {}).get("credits")
    print(f"submitted   : {task_id}" + (f"   (~{est} credits)" if est else ""))

    final = wait_for(key, task_id)
    if final.get("status") != "SUCCEEDED":
        # A refusal is a result, not a crash — and it cost nothing.
        print(f"\nFAILED: {final.get('failure') or final.get('status')}")
        code = final.get("failureCode")
        if code:
            print(f"code   : {code}")
        spent = (final.get("cost") or {}).get("credits", 0)
        print(f"credits: {spent}")
        sys.exit(1)

    outputs = final.get("output") or []
    if not outputs:
        sys.exit(f"SUCCEEDED but no output URL: {final}")

    dest = args.out or args.prompt_file.with_name(
        f"{args.prompt_file.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4"
    )
    download(outputs[0], dest)
    size = dest.stat().st_size
    print(f"\nsaved       : {dest}  ({size / 1_048_576:.1f} MB)")
    spent = (final.get("cost") or {}).get("credits")
    if spent is not None:
        print(f"credits     : {spent}")


if __name__ == "__main__":
    main()
