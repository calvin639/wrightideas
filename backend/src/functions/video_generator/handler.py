"""
SQS trigger: Video Generator

Processes each file in an order by submitting it to Runway ML Gen-3
for image-to-video conversion. Each submission is fire-and-forget —
Runway calls our webhook (/webhooks/runway) when each clip is ready.

SQS message format:
{
  "order_id": "uuid",
  "event": "payment_confirmed"
}
"""

import json
import os
import logging
import requests
import boto3

from shared.db import (
    get_order, get_order_files, update_order_status,
    update_file_status
)
from shared.models import OrderStatus, FileStatus
from shared.secrets import get_runway_key, get_runway_webhook_url

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RUNWAY_API_BASE = "https://api.dev.runwayml.com/v1"
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET")

s3 = boto3.client("s3")
S3_PRESIGN_EXPIRY = 3600  # 1 hour for Runway to fetch the image


def lambda_handler(event, context):
    results = {"batchItemFailures": []}

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            body = json.loads(record["body"])
            _process_order(body["order_id"])
        except Exception as e:
            logger.error(f"Failed to process record {message_id}: {e}", exc_info=True)
            results["batchItemFailures"].append({"itemIdentifier": message_id})

    return results


def _process_order(order_id: str) -> None:
    """Submit all files in an order to Runway ML."""
    order = get_order(order_id)
    if not order:
        raise ValueError(f"Order {order_id} not found")

    if order.status not in (OrderStatus.PAID, OrderStatus.PROCESSING):
        logger.info(f"Skipping order {order_id} with status {order.status}")
        return

    files = get_order_files(order_id)
    if not files:
        raise ValueError(f"No files found for order {order_id}")

    # Mark order as PROCESSING
    update_order_status(order_id, OrderStatus.PROCESSING)
    logger.info(f"Processing {len(files)} files for order {order_id}")

    submitted = 0
    for f in files:
        if f.status in (FileStatus.PROCESSING, FileStatus.DONE):
            logger.info(f"File {f.file_id} already {f.status}, skipping")
            continue

        try:
            task_id = _submit_to_runway(f)
            update_file_status(
                order_id, f.file_id,
                FileStatus.PROCESSING,
                runway_task_id=task_id,
            )
            submitted += 1
            logger.info(f"File {f.file_id} submitted to Runway: task {task_id}")
        except Exception as e:
            logger.error(f"Failed to submit file {f.file_id} to Runway: {e}")
            update_file_status(
                order_id, f.file_id,
                FileStatus.FAILED,
                error_message=str(e),
            )

    logger.info(f"Order {order_id}: submitted {submitted}/{len(files)} files to Runway")


def _submit_to_runway(file) -> str:
    """
    Submit a single file to Runway ML image_to_video endpoint.
    Returns the Runway task ID.

    Runway Gen-3 Alpha API docs:
    https://docs.dev.runwayml.com/api/image-to-video
    """
    # Generate a presigned URL so Runway can fetch the image from S3
    image_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": UPLOADS_BUCKET, "Key": file.s3_key},
        ExpiresIn=S3_PRESIGN_EXPIRY,
    )

    # Build a sensitive, appropriate prompt for memorial content
    loved_one_context = ""  # Could be enhanced with loved one name from order
    caption = file.caption.strip() if file.caption else ""
    prompt = _build_prompt(caption, file.content_type)

    payload = {
        "model": "gen3a_turbo",       # Fastest model; use "gen3a" for higher quality
        "promptImage": image_url,
        "promptText": prompt,
        "duration": 5,                 # 5-second clip
        "ratio": "1280:768",           # landscape (Runway's closest to 16:9)
        "webhookUrl": get_runway_webhook_url(),
    }

    headers = {
        "Authorization": f"Bearer {get_runway_key()}",
        "Content-Type": "application/json",
        "X-Runway-Version": "2024-11-06",
    }

    resp = requests.post(
        f"{RUNWAY_API_BASE}/image_to_video",
        json=payload,
        headers=headers,
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Runway API error {resp.status_code}: {resp.text[:500]}"
        )

    task_id = resp.json().get("id")
    if not task_id:
        raise RuntimeError(f"Runway returned no task ID: {resp.text}")

    return task_id


def _build_prompt(caption: str, content_type: str) -> str:
    """Build a tasteful, memorial-appropriate Runway prompt."""
    if caption:
        base = f"Gentle, cinematic memorial video. {caption}."
    else:
        base = "Gentle, cinematic, warm memorial tribute video."

    base += (
        " Soft lighting, slow pan, tender and respectful mood. "
        "No text overlays. High quality, emotional."
    )
    return base
