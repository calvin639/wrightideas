#!/usr/bin/env python3
"""
Probe which fal-hosted video models accept a REAL-PERSON photo with
first+last keyframe pinning — the capability Runway's seedance2 route blocks
(INPUT_PREPROCESSING.SAFETY.THIRD_PARTY).

Two candidates, both taking start + end images:

    bytedance/seedance-2.0/image-to-video      image_url + end_image_url
    fal-ai/kling-video/v3/pro/image-to-video   start_image_url + end_image_url

Usage (stdlib only — no pip installs needed):

    export FAL_KEY=...            # fal.ai dashboard -> Keys
    python3 scripts/test_keyframe_providers.py /path/to/face_photo.jpg
    python3 scripts/test_keyframe_providers.py photo.jpg --only kling

Use a photo WITH a clearly visible real face — the entire point is testing
the safety filter, so a landscape proves nothing. A prepared 1280x720 frame
is ideal (matches production), but any JPEG under ~4MB works.

Each test costs roughly $0.30-1.00 in fal credits. The script sends the same
image as both first and last frame (the identity-pinning technique) with a
minimal motion prompt, polls to a terminal state, and prints a verdict table.

The interesting outcomes:
    seedance PASSES  -> keep seedance, switch provider to fal (moderation is
                        per-platform configuration; fal's differs from
                        Runway's route)
    seedance BLOCKED, kling PASSES -> Kling v3 becomes the pipeline model
                        (also supports `elements` character references)
    both BLOCKED     -> Vidu Q3 start-end is the next candidate, and the
                        BytePlus real-human authorization program is the
                        long-term route
"""

# `dict | None` in an annotation is evaluated at def time before 3.10, and the
# system python3 on macOS is still 3.9.6 — without this the script dies on
# import with "unsupported operand type(s) for |". Keeps it true to its
# "stdlib only, runs anywhere" promise.
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

QUEUE = "https://queue.fal.run"

MODELS = {
    "seedance": {
        "app": "bytedance/seedance-2.0/image-to-video",
        "payload": lambda img: {
            "prompt": (
                "The camera slowly pushes in. The subject blinks softly and "
                "a faint smile forms."
            ),
            "image_url": img,
            "end_image_url": img,
            "resolution": "720p",
            "duration": 5,
        },
    },
    "kling": {
        "app": "fal-ai/kling-video/v3/pro/image-to-video",
        "payload": lambda img: {
            "prompt": (
                "The camera slowly pushes in. The subject blinks softly and "
                "a faint smile forms."
            ),
            "start_image_url": img,
            "end_image_url": img,
            "duration": 5,
            # Kling defaults this to true. The montage strips clip audio and
            # lays its own track over the top, so generated audio is paid for
            # and thrown away — and it makes this a like-for-like comparison
            # with Runway, whose clips are silent.
            "generate_audio": False,
        },
    },
}


def _req(url: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Key {key}",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:800]
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from None


def run_test(name: str, key: str, data_uri: str) -> dict:
    m = MODELS[name]
    print(f"\n=== {name}: submitting to {m['app']} ===")
    try:
        sub = _req(f"{QUEUE}/{m['app']}", key, m["payload"](data_uri))
    except RuntimeError as e:
        # A 4xx at submit time IS a result — record it, don't crash the run.
        print(f"  submit rejected: {e}")
        return {"model": name, "verdict": "REJECTED_AT_SUBMIT", "detail": str(e)}

    status_url = sub.get("status_url")
    response_url = sub.get("response_url")
    print(f"  queued: {sub.get('request_id', '?')}")

    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        time.sleep(10)
        st = _req(status_url, key)
        s = st.get("status")
        print(f"  {s}", flush=True)
        if s == "COMPLETED":
            out = _req(response_url, key)
            url = (out.get("video") or {}).get("url", "")
            print(f"  SUCCEEDED -> {url}")
            return {"model": name, "verdict": "SUCCEEDED", "video": url}
        if s in ("FAILED", "ERROR"):
            try:
                out = _req(response_url, key)
            except RuntimeError as e:
                out = {"error": str(e)}
            detail = json.dumps(out)[:600]
            blocked = "SAFETY" in detail.upper() or "MODERAT" in detail.upper()
            print(f"  FAILED: {detail}")
            return {
                "model": name,
                "verdict": "SAFETY_BLOCKED" if blocked else "FAILED",
                "detail": detail,
            }
    return {"model": name, "verdict": "TIMED_OUT"}


def main() -> None:
    key = os.environ.get("FAL_KEY", "")
    if not key:
        sys.exit("Set FAL_KEY (fal.ai dashboard -> Keys) and re-run.")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    photo = args[0]
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    # An http(s) argument is passed through untouched. That lets the probe send
    # the exact presigned S3 URL production sends to Runway, so a difference in
    # outcome is the provider's, not the delivery mechanism's. A local path is
    # still inlined as a data URI, which avoids needing the file to be public.
    if photo.startswith(("http://", "https://")):
        data_uri = photo
        print(f"photo: presigned URL ({photo.split('?')[0]}), pinned first+last")
    else:
        raw = open(photo, "rb").read()
        if len(raw) > 4 * 1024 * 1024:
            sys.exit(f"{photo} is {len(raw)//1024}KB — use a JPEG under 4MB "
                     "(a prepared 1280x720 frame is ideal).")
        data_uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        print(f"photo: {photo} ({len(raw)//1024}KB), same frame pinned first+last")

    results = []
    for name in MODELS:
        if only and name != only:
            continue
        results.append(run_test(name, key, data_uri))

    print("\n" + "=" * 52)
    for r in results:
        print(f"  {r['model']:<10} {r['verdict']}")
    print("=" * 52)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
