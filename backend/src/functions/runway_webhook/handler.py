"""
POST /webhooks/runway

Runway ML calls this endpoint when an image-to-video task completes
(or fails). We download the output clip, store it in S3, and check
if all files for the order are done — if so, we trigger the montage.

Runway webhook payload (approximate):
{
  "id": "task_abc123",
  "status": "SUCCEEDED" | "FAILED",
  "output": ["https://runway-output-url.mp4"],
  "error": "..."  // only on failure
}
"""

import json
import os
import logging
import requests
import boto3

from shared.db import (
    get_file_by_runway_task,
    update_file_status,
    update_order_status,
    all_files_complete,
    any_file_failed,
)
from shared.models import FileStatus, OrderStatus
from shared.response import ok, error, server_error

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET")
MONTAGE_QUEUE_URL = os.environ.get("MONTAGE_QUEUE_URL")

s3 = boto3.client("s3")
sqs = boto3.client("sqs")


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error("Invalid JSON")

    task_id = body.get("id")
    status = body.get("status")
    outputs = body.get("output", [])

    logger.info(f"Runway webhook: task={task_id} status={status}")

    if not task_id or not status:
        return error("Missing id or status")

    # Look up which file this task belongs to
    try:
        order_file = get_file_by_runway_task(task_id)
    except Exception as e:
        logger.error(f"DB error looking up task {task_id}: {e}")
        return server_error()

    if not order_file:
        logger.warning(f"No file found for Runway task {task_id} — may already be processed")
        return ok({"received": True})

    order_id = order_file.order_id
    file_id = order_file.file_id

    # ── Handle failure ────────────────────────────────────────────────────────
    if status == "FAILED":
        err_msg = body.get("error", "Runway task failed")
        logger.error(f"Runway task {task_id} FAILED for file {file_id}: {err_msg}")
        update_file_status(order_id, file_id, FileStatus.FAILED, error_message=err_msg)
        _check_order_completion(order_id)
        return ok({"received": True})

    # ── Handle success ────────────────────────────────────────────────────────
    if status == "SUCCEEDED" and outputs:
        video_url = outputs[0]  # Runway returns a list; first item is the video

        # Download clip and re-upload to our S3 (Runway URLs expire)
        try:
            clip_key = _download_and_store_clip(video_url, order_id, file_id)
            logger.info(f"Clip stored at s3://{VIDEOS_BUCKET}/{clip_key}")
        except Exception as e:
            logger.error(f"Failed to store clip for {file_id}: {e}")
            update_file_status(order_id, file_id, FileStatus.FAILED, error_message=str(e))
            return server_error("Failed to store generated clip")

        update_file_status(
            order_id, file_id,
            FileStatus.DONE,
            generated_video_s3_key=clip_key,
        )

        # Check if this was the last file
        _check_order_completion(order_id)

    return ok({"received": True})


def _download_and_store_clip(runway_url: str, order_id: str, file_id: str) -> str:
    """Download a Runway-generated clip and store it in our S3 videos bucket."""
    resp = requests.get(runway_url, stream=True, timeout=120)
    resp.raise_for_status()

    s3_key = f"clips/{order_id}/{file_id}.mp4"
    s3.put_object(
        Bucket=VIDEOS_BUCKET,
        Key=s3_key,
        Body=resp.content,
        ContentType="video/mp4",
    )
    return s3_key


def _check_order_completion(order_id: str) -> None:
    """Check if all files are processed and trigger montage if so."""
    try:
        if all_files_complete(order_id):
            logger.info(f"All clips ready for order {order_id} — triggering montage")
            update_order_status(order_id, OrderStatus.MONTAGE)
            sqs.send_message(
                QueueUrl=MONTAGE_QUEUE_URL,
                MessageBody=json.dumps({"order_id": order_id}),
            )
        elif any_file_failed(order_id):
            # Some files failed — we still attempt montage with what we have
            # if at least one clip is available, otherwise mark as failed
            from shared.db import get_order_files
            done_files = [
                f for f in get_order_files(order_id)
                if f.status == FileStatus.DONE
            ]
            if done_files:
                logger.warning(
                    f"Order {order_id} has some failed files but {len(done_files)} "
                    f"clips succeeded — proceeding with partial montage"
                )
                update_order_status(order_id, OrderStatus.MONTAGE)
                sqs.send_message(
                    QueueUrl=MONTAGE_QUEUE_URL,
                    MessageBody=json.dumps({"order_id": order_id, "partial": True}),
                )
            else:
                logger.error(f"Order {order_id} — all files failed")
                update_order_status(
                    order_id, OrderStatus.FAILED,
                    error_message="All video generation attempts failed"
                )
    except Exception as e:
        logger.error(f"Error checking order completion for {order_id}: {e}")
