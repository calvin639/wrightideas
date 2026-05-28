#!/usr/bin/env python3
"""
End-to-end pipeline test — exercises the full real-customer flow:

  1. Discovers photos in a local folder (default: backend/test_photos/).
  2. POSTs /orders to create an order and get a presigned PUT URL per file.
  3. Uploads each local photo to S3 via its presigned URL — same as the
     real frontend does.
  4. POSTs /orders/{id}/checkout to create a real Stripe (test mode)
     Checkout session, opens the URL, prints the test card to use.
  5. After payment, Stripe's real webhook fires and the pipeline runs
     end-to-end — delivering BOTH transactional emails:
       • send_order_confirmation  (stripe_webhook handler on PAID)
       • send_video_ready         (montage_builder handler on COMPLETE)

Usage:
  # Drop a few photos in backend/test_photos/ first, then:
  python3 scripts/test_video_pipeline.py

  # Choose background music (one of: none | beautiful | emotion | nature):
  python3 scripts/test_video_pipeline.py --music nature

  # Override the photos folder:
  TEST_PHOTOS_DIR=/path/to/photos python3 scripts/test_video_pipeline.py

After completing checkout in the browser, run
./scripts/check_pipeline.sh <order_id> to watch progress.

Heads-up: each photo becomes one Runway clip, which costs real money on
your Runway API key. Keep the test folder modest (e.g. 3–5 photos).
"""

import argparse
import mimetypes
import os
import sys
import webbrowser
from pathlib import Path

import boto3
import requests

# Must align with VALID_MUSIC in create_order/handler.py
VALID_MUSIC_CHOICES = {"none", "beautiful", "emotion", "nature"}

# ── Config ────────────────────────────────────────────────────────────────────

STACK_NAME = "memories-in-stone-dev"
REGION = "eu-west-1"

# Local folder of test photos. Anything matching ALLOWED_EXTS is uploaded.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PHOTOS_DIR = SCRIPT_DIR.parent / "test_photos"
TEST_PHOTOS_DIR = Path(os.environ.get("TEST_PHOTOS_DIR", DEFAULT_PHOTOS_DIR))

# Must align with ALLOWED_TYPES in create_order/handler.py
ALLOWED_EXTS = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".mp4":  "video/mp4",
    ".mov":  "video/quicktime",
}

# Test customer — everything except `files` (built from disk) and
# `music_choice` (parsed from CLI; defaults to "none").
ORDER_BASE = {
    "customer_name": "Calvin Wright",
    "customer_email": "calvin@wrightideas.biz",
    "customer_phone": "+353871234567",
    "loved_one_name": "Test Subject",
    "loved_one_dob": "1945-03-12",
    "loved_one_dod": "2024-11-20",
    "stone_message": "Forever in our hearts",
    "stone_style": "black_slate",
    "stone_quantity": 1,
}


def _parse_args():
    p = argparse.ArgumentParser(description="End-to-end Memories in Stone test order")
    p.add_argument(
        "--music",
        default=os.environ.get("MUSIC_CHOICE", "none"),
        choices=sorted(VALID_MUSIC_CHOICES),
        help="Background music track for the montage (default: none)",
    )
    return p.parse_args()


# ── Helpers ───────────────────────────────────────────────────────────────────

def cf_outputs(stack_name: str, region: str) -> dict:
    cf = boto3.client("cloudformation", region_name=region)
    resp = cf.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def discover_photos(folder: Path) -> list[tuple[Path, str]]:
    """Return [(path, content_type), ...] for every supported file in folder.

    Sorted by filename so order is deterministic and matches what a customer
    would see in a normal directory listing.
    """
    if not folder.exists():
        return []
    out: list[tuple[Path, str]] = []
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        ct = ALLOWED_EXTS.get(p.suffix.lower())
        if not ct:
            continue
        # Prefer mimetypes when it agrees (handles edge cases); fall back to our table
        guess, _ = mimetypes.guess_type(p.name)
        out.append((p, guess if guess in ALLOWED_EXTS.values() else ct))
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args = _parse_args()
    music_choice = args.music
    print(f"── Test config ──────────────────────────────────────────────────────")
    print(f"   Music choice: {music_choice}")
    if music_choice != "none":
        print(f"   (Asset must exist at s3://VIDEOS_BUCKET/music/{music_choice}.mp3,")
        print(f"    otherwise montage_builder skips audio with a warning.)")

    print("\n── Stack outputs ────────────────────────────────────────────────────")
    try:
        outs = cf_outputs(STACK_NAME, REGION)
    except Exception as e:
        print(f"❌ Could not read stack outputs: {e}")
        sys.exit(1)

    api_url       = outs.get("ApiUrl", "").rstrip("/")

    if not api_url:
        print("❌ Missing stack output: ApiUrl")
        sys.exit(1)
    print(f"   ApiUrl = {api_url}")

    webhook_url = f"{api_url}/webhooks/stripe"
    print(f"\n   Expected Stripe webhook URL (must be configured in Stripe Dashboard):")
    print(f"     {webhook_url}")

    # Check Runway model currently on the Lambda
    try:
        lam = boto3.client("lambda", region_name=REGION)
        cfg = lam.get_function_configuration(FunctionName=f"memories-video-generator-dev")
        model = cfg.get("Environment", {}).get("Variables", {}).get("RUNWAY_MODEL", "unknown")
        print(f"\n   RUNWAY_MODEL on Lambda: {model}")
    except Exception:
        pass

    # ── 1. Discover local test photos ─────────────────────────────────────────
    print(f"\n── Discovering photos in {TEST_PHOTOS_DIR} ─────────────────────────")
    photos = discover_photos(TEST_PHOTOS_DIR)
    if not photos:
        print(f"❌ No supported photos/videos found in {TEST_PHOTOS_DIR}")
        print(f"   Supported extensions: {sorted(ALLOWED_EXTS)}")
        print(f"   Drop a few files in that folder and re-run.")
        sys.exit(1)
    print(f"   Found {len(photos)} file(s):")
    for p, ct in photos:
        print(f"     {p.name}  ({ct}, {p.stat().st_size:,} bytes)")

    # ── 2. Create order via API ───────────────────────────────────────────────
    print("\n── Creating order ───────────────────────────────────────────────────")
    files_payload = [
        {"filename": p.name, "content_type": ct, "caption": f"Photo {i+1}"}
        for i, (p, ct) in enumerate(photos)
    ]
    order_payload = {**ORDER_BASE, "music_choice": music_choice, "files": files_payload}
    resp = requests.post(f"{api_url}/orders", json=order_payload, timeout=30)
    if resp.status_code not in (200, 201):
        print(f"❌ POST /orders failed {resp.status_code}: {resp.text[:500]}")
        sys.exit(1)

    data = resp.json()
    order_id = data["order_id"]
    files = data["files"]  # list of {file_id, s3_key, upload_url, ...}
    print(f"   ✅ Order created: {order_id}")
    print(f"   Total: €{data.get('total_amount_euros', '?')}  Stones: {data.get('stone_quantity', '?')}")

    if len(files) != len(photos):
        print(f"❌ API returned {len(files)} file slots but we have {len(photos)} photos — aborting")
        sys.exit(1)

    # ── 3. Upload each photo via its presigned URL (real customer flow) ──────
    print("\n── Uploading photos via presigned PUT URLs ──────────────────────────")
    for (local_path, content_type), file_info in zip(photos, files):
        upload_url = file_info["upload_url"]
        s3_key     = file_info["s3_key"]
        try:
            with open(local_path, "rb") as fh:
                body = fh.read()
            # IMPORTANT: Content-Type must match what was sent in the /orders
            # request — the presigned URL is signed against it, otherwise S3
            # returns SignatureDoesNotMatch.
            put = requests.put(
                upload_url,
                data=body,
                headers={"Content-Type": content_type},
                timeout=120,
            )
            if put.status_code not in (200, 204):
                print(f"   ❌ Upload failed for {local_path.name} ({put.status_code}): {put.text[:300]}")
                sys.exit(1)
            print(f"   ✅ {local_path.name}  →  s3://.../{s3_key}  ({len(body):,} bytes)")
        except Exception as e:
            print(f"   ❌ Upload error for {local_path.name}: {e}")
            sys.exit(1)

    # ── 4. Create real Stripe Checkout session (test mode) ───────────────────
    print("\n── Creating Stripe Checkout session ─────────────────────────────────")
    try:
        co_resp = requests.post(f"{api_url}/orders/{order_id}/checkout", timeout=30)
    except Exception as e:
        print(f"   ❌ POST /orders/{order_id}/checkout failed: {e}")
        sys.exit(1)

    if co_resp.status_code not in (200, 201):
        print(f"   ❌ Checkout creation failed {co_resp.status_code}: {co_resp.text[:500]}")
        sys.exit(1)

    co_data = co_resp.json()
    checkout_url = co_data.get("checkout_url")
    session_id   = co_data.get("session_id")
    if not checkout_url:
        print(f"   ❌ Response missing checkout_url: {co_data}")
        sys.exit(1)

    print(f"   ✅ Stripe Checkout session created: {session_id}")
    print(f"\n   Open this URL in your browser to complete payment:")
    print(f"     {checkout_url}\n")
    print(f"   Stripe test card details (test mode only):")
    print(f"     Card number : 4242 4242 4242 4242")
    print(f"     Expiry      : any future date (e.g. 12/30)")
    print(f"     CVC         : any 3 digits (e.g. 123)")
    print(f"     ZIP / name  : anything")

    # Try to auto-open the URL (best effort — silently no-op if no browser available)
    try:
        webbrowser.open(checkout_url)
    except Exception:
        pass

    # ── Done ─────────────────────────────────────────────────────────────────
    model_str = model if "model" in dir() else "check Lambda env"
    print(f"""
── Pipeline armed ───────────────────────────────────────────────────
   Order ID : {order_id}
   Photos   : {len(photos)} (one Runway clip each)
   Music    : {music_choice}
   Model    : {model_str}
   Customer : {ORDER_BASE['customer_email']}

After you complete payment in the browser:
   - Stripe fires checkout.session.completed → API GW → StripeWebhookFunction
   - StripeWebhookFunction sends:
       • send_order_confirmation  → {ORDER_BASE['customer_email']}
       • send_admin_new_order     → admin (see ADMIN_EMAIL env)
   - then queues video generation. When montage finishes,
     MontageBuilderFunction sends:
       • send_video_ready         → {ORDER_BASE['customer_email']}

Monitor progress:
   ./scripts/check_pipeline.sh {order_id}

Lambda logs (tail):
   aws logs tail /aws/lambda/memories-stripe-webhook-dev   --follow --region {REGION}
   aws logs tail /aws/lambda/memories-video-generator-dev  --follow --region {REGION}
   aws logs tail /aws/lambda/memories-runway-poller-dev    --follow --region {REGION}
   aws logs tail /aws/lambda/memories-montage-builder-dev  --follow --region {REGION}

If you DON'T see the order-confirmation email within ~30 seconds of paying,
the Stripe webhook is likely not configured — verify in Stripe Dashboard
that the endpoint above is registered and the signing secret matches the
one stored at SSM /memories/dev/stripe-webhook-secret.
""")


if __name__ == "__main__":
    main()
