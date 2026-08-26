"""
Step Functions task: Review Gate notification (waitForTaskToken callback).

Invoked between PrepareFiles and GenerateClips with the execution's task token.
Emails the admin a review of every prepared frame — before/after pairs, the
restoration decisions that were made, and any upload-quality warnings — with
links to approve the order or swap individual files to their unenhanced
versions. The state machine waits on the token, so nothing reaches Runway (and
nothing is spent) until a human approves or the review window times out.

REVIEW_MODE:
    off     — approve immediately, send nothing. Zero-cost bypass.
    notify  — approve immediately but still send the email (FYI only).
    gate    — send the email and WAIT for the decision endpoint to redeem the
              token. The state's TimeoutSeconds bounds the wait; on timeout the
              state machine auto-approves via its Catch, so a missed email can
              never break the delivery promise.

Input (from the state machine):
    {"order_id": "uuid", "task_token": "..."}
"""

import json
import logging
import os
import uuid

import boto3

from shared.db import get_order, get_order_files, update_order_status
from shared.email_utils import send_admin_review_request
from shared.models import FileStatus, MediaKind

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REVIEW_MODE = os.environ.get("REVIEW_MODE", "gate").lower()
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
REVIEW_API_BASE = os.environ.get("REVIEW_API_BASE", "").rstrip("/")

# Presigned URLs in the review email must outlive the review window (the
# state's TimeoutSeconds, default 12h) or the images break mid-review.
PRESIGN_EXPIRY = int(os.environ.get("REVIEW_PRESIGN_EXPIRY", str(24 * 3600)))

s3 = boto3.client("s3")
sfn = boto3.client("stepfunctions")


def lambda_handler(event, context):
    order_id = event["order_id"]
    task_token = event["task_token"]

    order = get_order(order_id)
    if not order:
        raise ValueError(f"Order {order_id} not found")

    if REVIEW_MODE == "off":
        _approve_now(task_token, order_id, "review disabled")
        return {"review": "auto_approved", "mode": REVIEW_MODE}

    if REVIEW_MODE != "gate":
        # notify: email is informational; the pipeline does not wait. Images
        # are direct presigns — fine at this lifetime, nothing depends on them.
        _approve_now(task_token, order_id, "notify mode")
        items = _build_review_items(order_id, decide_base="")
        _send_email(order, items, decide_base="")
        return {"review": "auto_approved", "mode": REVIEW_MODE}

    # gate: store the token BEFORE sending (no click-before-store race), and
    # if the email cannot be sent, redeem the token immediately — an unsent
    # review email must cost zero minutes, not the full review window.
    review_key = uuid.uuid4().hex
    update_order_status(
        order_id, order.status,
        review_key=review_key,
        review_task_token=task_token,
        review_status="PENDING",
    )
    decide_base = (
        f"{REVIEW_API_BASE}/review/decide?order={order_id}&key={review_key}"
        if REVIEW_API_BASE else ""
    )
    # Email images are served THROUGH the decision endpoint as short-lived
    # redirects rather than long presigned URLs: a presigned URL dies with the
    # Lambda session credentials that signed it, so a "24 hour" URL can 403
    # hours before a 12-hour review window closes.
    items = _build_review_items(order_id, decide_base=decide_base)

    if not decide_base or not _send_email(order, items, decide_base=decide_base):
        logger.error(
            "Review email not sent for %s — auto-approving rather than stalling",
            order_id,
        )
        _approve_now(task_token, order_id, "review email failed")
        return {"review": "auto_approved", "mode": REVIEW_MODE, "email": "failed"}

    logger.info("Order %s waiting on review (%s files)", order_id, len(items))
    return {"review": "pending", "files": len(items)}


def _approve_now(task_token: str, order_id: str, reason: str) -> None:
    sfn.send_task_success(
        taskToken=task_token,
        output=json.dumps({"approved": True, "reason": reason}),
    )
    order = get_order(order_id)
    update_order_status(order_id, order.status, review_status="AUTO_APPROVED")
    logger.info("Order %s auto-approved (%s)", order_id, reason)


def _build_review_items(order_id: str, decide_base: str) -> list:
    """One review row per prepared image: URLs, warnings, restore decisions.

    With decide_base (gate mode) image URLs go through the decision endpoint,
    which validates the review key and 302-redirects to a fresh short-lived
    presign on every load — valid for the whole review window. Without it
    (notify mode) plain presigns are used.
    """
    items = []
    for f in get_order_files(order_id):
        if f.media_kind != MediaKind.IMAGE or f.status != FileStatus.PREPARED:
            continue
        if not f.prepared_s3_key:
            continue
        before_key = f"prepared/{order_id}/{f.file_id}_before.jpg"
        has_before = _exists(before_key)
        if decide_base:
            after_url = f"{decide_base}&action=image&file={f.file_id}&which=after"
            before_url = (
                f"{decide_base}&action=image&file={f.file_id}&which=before"
                if has_before else ""
            )
        else:
            after_url = _presign(f.prepared_s3_key)
            before_url = _presign(before_key) if has_before else ""
        items.append({
            "file_id": f.file_id,
            "filename": f.original_filename,
            "after_url": after_url,
            "before_url": before_url,
            "warnings": (f.assessment or {}).get("warnings", []),
            "restore_meta": f.restore_meta or {},
        })
    return items


def _presign(key: str) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": UPLOADS_BUCKET, "Key": key},
        ExpiresIn=PRESIGN_EXPIRY,
    )


def _exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=UPLOADS_BUCKET, Key=key)
        return True
    except Exception:
        return False


def _send_email(order, items, decide_base: str) -> bool:
    try:
        return send_admin_review_request(order, items, decide_base)
    except Exception as e:
        logger.error("Review email failed for %s: %s", order.order_id, e, exc_info=True)
        return False
