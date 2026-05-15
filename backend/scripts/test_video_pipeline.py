#!/usr/bin/env python3
"""
End-to-end pipeline test — creates a real order, reuses uploaded photos
from a reference S3 folder, simulates payment, and lets the full pipeline run.

Usage:
  python3 scripts/test_video_pipeline.py

The script prints the order ID and a monitoring command at the end.
Run ./scripts/check_pipeline.sh <order_id> at any point to inspect progress.
"""

import json
import subprocess
import sys
import time

import boto3
import requests

# ── Config ────────────────────────────────────────────────────────────────────

STACK_NAME = "memories-in-stone-dev"
REGION = "eu-west-1"

# Reference order whose uploaded photos we'll reuse
REF_ORDER_ID = "61bbbe5a-e7e6-4334-a41a-75d67d31a900"

# Test customer — use a @wrightideas.biz address so SES delivers the email
ORDER_PAYLOAD = {
    "customer_name": "Calvin Wright",
    "customer_email": "calvin@wrightideas.biz",
    "customer_phone": "+353871234567",
    "loved_one_name": "Test Subject",
    "loved_one_dob": "1945-03-12",
    "loved_one_dod": "2024-11-20",
    "stone_message": "Forever in our hearts",
    "stone_style": "black_slate",
    "stone_quantity": 1,
    "files": [
        {"filename": "IMG_5381.jpg", "content_type": "image/jpeg", "caption": "Family photo 1"},
        {"filename": "IMG_5831.jpg", "content_type": "image/jpeg", "caption": "Family photo 2"},
        {"filename": "IMG_5974.jpg", "content_type": "image/jpeg", "caption": "Family photo 3"},
        {"filename": "IMG_6428.jpg", "content_type": "image/jpeg", "caption": "Family photo 4"},
    ],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def cf_outputs(stack_name: str, region: str) -> dict:
    cf = boto3.client("cloudformation", region_name=region)
    resp = cf.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def dynamo_set_paid(table_name: str, order_id: str, region: str) -> None:
    db = boto3.resource("dynamodb", region_name=region)
    table = db.Table(table_name)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    table.update_item(
        Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
        UpdateExpression="SET #st = :s, updated_at = :ts, GSI1PK = :gsi",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":s": "PAID", ":ts": now, ":gsi": "STATUS#PAID"},
    )


def sqs_trigger_video_gen(queue_url: str, order_id: str, region: str) -> None:
    sqs = boto3.client("sqs", region_name=region)
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"order_id": order_id, "event": "payment_confirmed"}),
    )


def s3_copy(src_bucket: str, src_key: str, dst_bucket: str, dst_key: str, region: str) -> None:
    s3 = boto3.client("s3", region_name=region)
    s3.copy_object(
        CopySource={"Bucket": src_bucket, "Key": src_key},
        Bucket=dst_bucket,
        Key=dst_key,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("── Stack outputs ────────────────────────────────────────────────────")
    try:
        outs = cf_outputs(STACK_NAME, REGION)
    except Exception as e:
        print(f"❌ Could not read stack outputs: {e}")
        sys.exit(1)

    api_url       = outs.get("ApiUrl", "").rstrip("/")
    uploads_bucket = outs.get("UploadsBucketName")
    orders_table   = outs.get("OrdersTableName")
    video_queue    = outs.get("VideoGenerationQueueUrl")

    for k, v in [("ApiUrl", api_url), ("UploadsBucketName", uploads_bucket),
                  ("OrdersTableName", orders_table), ("VideoGenerationQueueUrl", video_queue)]:
        if not v:
            print(f"❌ Missing stack output: {k}")
            sys.exit(1)
        print(f"   {k} = {v}")

    # Check Runway model currently on the Lambda
    try:
        lam = boto3.client("lambda", region_name=REGION)
        cfg = lam.get_function_configuration(FunctionName=f"memories-video-generator-dev")
        model = cfg.get("Environment", {}).get("Variables", {}).get("RUNWAY_MODEL", "unknown")
        print(f"\n   RUNWAY_MODEL on Lambda: {model}")
    except Exception:
        pass

    # ── 1. Create order via API ───────────────────────────────────────────────
    print("\n── Creating order ───────────────────────────────────────────────────")
    resp = requests.post(f"{api_url}/orders", json=ORDER_PAYLOAD, timeout=30)
    if resp.status_code not in (200, 201):
        print(f"❌ POST /orders failed {resp.status_code}: {resp.text[:500]}")
        sys.exit(1)

    data = resp.json()
    order_id = data["order_id"]
    files = data["files"]  # list of {file_id, s3_key, upload_url, ...}
    print(f"   ✅ Order created: {order_id}")
    print(f"   Files ({len(files)}):")
    for f in files:
        print(f"     {f['file_id'][:8]}  {f['s3_key']}")

    # ── 2. Copy reference photos to new order's S3 keys ──────────────────────
    print("\n── Copying photos from reference order ──────────────────────────────")
    ref_filenames = ["IMG_5381.jpg", "IMG_5831.jpg", "IMG_5974.jpg", "IMG_6428.jpg"]

    for i, file_info in enumerate(files):
        src_filename = ref_filenames[i % len(ref_filenames)]
        src_key = f"orders/{REF_ORDER_ID}/{src_filename}"
        dst_key = file_info["s3_key"]
        try:
            s3_copy(uploads_bucket, src_key, uploads_bucket, dst_key, REGION)
            print(f"   ✅ {src_filename}  →  {dst_key}")
        except Exception as e:
            print(f"   ❌ Failed to copy {src_filename}: {e}")
            sys.exit(1)

    # ── 3. Simulate payment: set order PAID + push to SQS ────────────────────
    print("\n── Simulating payment ───────────────────────────────────────────────")
    try:
        dynamo_set_paid(orders_table, order_id, REGION)
        print("   ✅ Order status → PAID in DynamoDB")
    except Exception as e:
        print(f"   ❌ DynamoDB update failed: {e}")
        sys.exit(1)

    time.sleep(1)  # tiny pause so DynamoDB write is visible before SQS fires

    try:
        sqs_trigger_video_gen(video_queue, order_id, REGION)
        print("   ✅ Message sent to VideoGenerationQueue")
    except Exception as e:
        print(f"   ❌ SQS send failed: {e}")
        sys.exit(1)

    # ── Done ─────────────────────────────────────────────────────────────────
    print(f"""
── Pipeline started ─────────────────────────────────────────────────
   Order ID : {order_id}
   Model    : {model if "model" in dir() else "check Lambda env"}
   Customer : {ORDER_PAYLOAD['customer_email']}

Monitor progress:
   ./scripts/check_pipeline.sh {order_id}

Lambda logs (tail):
   aws logs tail /aws/lambda/memories-video-generator-dev --follow --region {REGION}
   aws logs tail /aws/lambda/memories-runway-poller-dev  --follow --region {REGION}
   aws logs tail /aws/lambda/memories-montage-builder-dev --follow --region {REGION}

The poller runs every 5 min as a fallback if Runway webhooks don't arrive.
You should receive a video-ready email at {ORDER_PAYLOAD['customer_email']} when complete.
""")


if __name__ == "__main__":
    main()
