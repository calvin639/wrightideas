"""
EventBridge scheduled poller — catches Runway tasks that completed
without triggering our webhook.

Runway's dev API (api.dev.runwayml.com) doesn't reliably deliver webhook
callbacks, so this runs every 5 minutes as a fallback. It finds all
PROCESSING orders, polls each file's Runway task status, and drives
completed tasks through to the montage step.
"""

import json
import logging
import os

import boto3
import requests

from shared.db import (
    all_files_complete,
    any_file_failed,
    get_order,
    get_order_files,
    get_orders_by_status,
    update_file_status,
    update_order_status,
)
from shared.models import FileStatus, OrderStatus
from shared.secrets import get_runway_key

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RUNWAY_API_BASE = "https://api.dev.runwayml.com/v1"
VIDEOS_BUCKET = os.environ.get("VIDEOS_BUCKET")
MONTAGE_QUEUE_URL = os.environ.get("MONTAGE_QUEUE_URL")

s3 = boto3.client("s3")
sqs = boto3.client("sqs")


def lambda_handler(event, context):
    runway_key = get_runway_key()
    processing_orders = get_orders_by_status(OrderStatus.PROCESSING)
    logger.info(f"Polling: {len(processing_orders)} orders in PROCESSING state")

    for order in processing_orders:
        try:
            _check_order(order, runway_key)
        except Exception as e:
            logger.error(f"Error checking order {order.order_id}: {e}", exc_info=True)


def _check_order(order, runway_key: str) -> None:
    files = get_order_files(order.order_id)
    pending = [f for f in files if f.status == FileStatus.PROCESSING and f.runway_task_id]

    if not pending:
        return

    logger.info(f"Order {order.order_id}: polling {len(pending)} Runway tasks")
    for f in pending:
        try:
            _poll_file(order.order_id, f, runway_key)
        except Exception as e:
            logger.error(f"Failed polling file {f.file_id}: {e}", exc_info=True)

    _check_order_completion(order.order_id)


def _poll_file(order_id: str, file, runway_key: str) -> None:
    resp = requests.get(
        f"{RUNWAY_API_BASE}/tasks/{file.runway_task_id}",
        headers={"Authorization": f"Bearer {runway_key}", "X-Runway-Version": "2024-11-06"},
        timeout=30,
    )
    resp.raise_for_status()
    task = resp.json()
    status = task.get("status")

    if status == "SUCCEEDED":
        outputs = task.get("output", [])
        if not outputs:
            logger.warning(f"Task {file.runway_task_id} SUCCEEDED with no output URL")
            return
        clip_key = _download_and_store_clip(outputs[0], order_id, file.file_id)
        update_file_status(order_id, file.file_id, FileStatus.DONE, generated_video_s3_key=clip_key)
        logger.info(f"Poller: file {file.file_id} -> DONE")

    elif status == "FAILED":
        err = task.get("error", "Runway task failed")
        update_file_status(order_id, file.file_id, FileStatus.FAILED, error_message=err)
        logger.error(f"Poller: task {file.runway_task_id} FAILED: {err}")

    else:
        logger.info(f"Task {file.runway_task_id} still {status}")


def _download_and_store_clip(runway_url: str, order_id: str, file_id: str) -> str:
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
    # Re-read order to avoid triggering montage on orders already past PROCESSING
    current = get_order(order_id)
    if not current or current.status != OrderStatus.PROCESSING:
        return

    try:
        if all_files_complete(order_id):
            logger.info(f"Poller: all clips ready for {order_id} — triggering montage")
            update_order_status(order_id, OrderStatus.MONTAGE)
            sqs.send_message(
                QueueUrl=MONTAGE_QUEUE_URL,
                MessageBody=json.dumps({"order_id": order_id}),
            )
        elif any_file_failed(order_id):
            done = [f for f in get_order_files(order_id) if f.status == FileStatus.DONE]
            if done:
                logger.warning(f"Poller: partial montage for {order_id} ({len(done)} clips)")
                update_order_status(order_id, OrderStatus.MONTAGE)
                sqs.send_message(
                    QueueUrl=MONTAGE_QUEUE_URL,
                    MessageBody=json.dumps({"order_id": order_id, "partial": True}),
                )
            else:
                logger.error(f"Poller: all files failed for {order_id}")
                update_order_status(order_id, OrderStatus.FAILED, error_message="All video generation failed")
    except Exception as e:
        logger.error(f"Poller: completion check failed for {order_id}: {e}")
