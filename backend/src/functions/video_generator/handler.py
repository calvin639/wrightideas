"""
SQS trigger: Video Generator

Processes each file in an order by submitting it to Runway ML
for image-to-video conversion. Each submission is fire-and-forget —
Runway calls our webhook (/webhooks/runway) when each clip is ready.

For each image we first call Bedrock Claude Haiku with the image and a
"shot director" system prompt to generate a tailored motion prompt
following Runway's documented best practices. If Bedrock fails we fall
back to a safe generic motion prompt — the pipeline never blocks on the
prompt-generation step.

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
from shared.prompt_generator import generate_motion_prompt, FALLBACK_PROMPT

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RUNWAY_API_BASE = "https://api.dev.runwayml.com/v1"
RUNWAY_MODEL = os.environ.get("RUNWAY_MODEL", "gen4.5")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET")

s3 = boto3.client("s3")
S3_PRESIGN_EXPIRY = 3600  # 1 hour for Runway to fetch the image

# Landscape ratio per model — Runway validates this strictly and the accepted
# values differ between model families. gen3a_turbo takes 1280:768; gen4/gen4.5
# and the veo models take 1280:720 and reject 1280:768 with a 400.
DEFAULT_RATIO = "1280:720"
MODEL_RATIOS = {
    "gen3a_turbo": "1280:768",
}


def _ratio_for_model(model: str) -> str:
    """Landscape ratio Runway accepts for this model (see MODEL_RATIOS)."""
    return MODEL_RATIOS.get(model, DEFAULT_RATIO)

# Per-image prompt generation can be disabled via env var as a kill switch.
# When false, we fall back to a single generic motion prompt for every image.
USE_PER_IMAGE_PROMPTS = os.environ.get("USE_PER_IMAGE_PROMPTS", "true").lower() == "true"


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

    # Per-order model override (for testing); empty → env default.
    model = (getattr(order, "runway_model", "") or "").strip() or RUNWAY_MODEL
    logger.info(f"Processing {len(files)} files for order {order_id} with model '{model}'")

    submitted = 0
    for f in files:
        if f.status in (FileStatus.PROCESSING, FileStatus.DONE):
            logger.info(f"File {f.file_id} already {f.status}, skipping")
            continue

        prompt = ""
        try:
            # 1. Build the motion prompt for this specific image.
            #    Reuse a previously-generated prompt if we have one (idempotent
            #    on retries — saves a Bedrock call).
            prompt = f.runway_prompt or _build_prompt_for_file(f)
            logger.info(f"File {f.file_id} prompt: {prompt}")

            # 2. Submit to Runway with the tailored prompt.
            task_id = _submit_to_runway(f, prompt, model)

            # 3. Persist both the task ID and the prompt we used (for debugging).
            update_file_status(
                order_id, f.file_id,
                FileStatus.PROCESSING,
                runway_task_id=task_id,
                runway_prompt=prompt,
            )
            submitted += 1
            logger.info(
                f"File {f.file_id} submitted to Runway: task {task_id} "
                f"(prompt: {prompt[:80]}...)"
            )
        except Exception as e:
            logger.error(f"Failed to submit file {f.file_id} to Runway: {e}", exc_info=True)
            # Persist the prompt even on failure: it's already been paid for at
            # Bedrock, a retry can reuse it, and it's the only record of what we
            # actually sent when debugging a Runway rejection.
            update_file_status(
                order_id, f.file_id,
                FileStatus.FAILED,
                error_message=str(e),
                runway_prompt=prompt,
            )

    logger.info(f"Order {order_id}: submitted {submitted}/{len(files)} files to Runway")


def _build_prompt_for_file(file) -> str:
    """
    Generate a per-image motion prompt by calling Bedrock with the image and
    the customer's caption (as context). Falls back to a generic prompt on
    any error so we never block the pipeline.
    """
    if not USE_PER_IMAGE_PROMPTS:
        logger.info(f"Per-image prompts disabled, using fallback for {file.file_id}")
        return FALLBACK_PROMPT

    try:
        resp = s3.get_object(Bucket=UPLOADS_BUCKET, Key=file.s3_key)
        image_bytes = resp["Body"].read()
        content_type = file.content_type or resp.get("ContentType", "image/jpeg")
    except Exception as e:
        logger.error(
            f"Could not fetch {file.s3_key} from S3 for prompt generation: {e}"
        )
        return FALLBACK_PROMPT

    return generate_motion_prompt(
        image_bytes=image_bytes,
        content_type=content_type,
        caption=file.caption or "",
    )


def _submit_to_runway(file, prompt: str, model: str = RUNWAY_MODEL) -> str:
    """
    Submit a single file to Runway ML image_to_video endpoint.
    Returns the Runway task ID.

    `model` is resolved by the caller (per-order override or env default).

    Runway API docs: https://docs.dev.runwayml.com/api/image-to-video
    """
    # Generate a presigned URL so Runway can fetch the image from S3
    image_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": UPLOADS_BUCKET, "Key": file.s3_key},
        ExpiresIn=S3_PRESIGN_EXPIRY,
    )

    payload = {
        "model": model,
        "promptImage": image_url,
        "promptText": prompt,
        "duration": 5,                 # 5-second clip
        "ratio": _ratio_for_model(model),
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
