"""
GET /orders/{order_id}

Returns the current status and details of an order.
Used by the frontend order tracking page.

Response:
{
  "order_id": "uuid",
  "status": "PROCESSING",
  "loved_one_name": "Michael Murphy",
  "stone_quantity": 1,
  "total_amount_euros": 69.99,
  "created_at": "2025-03-01T...",
  "video_url": "",          // populated when status=COMPLETE
  "tribute_page_url": "",   // populated when status=COMPLETE
  "qr_code_url": "",        // populated when status=COMPLETE
  "files": [
    {"file_id": "...", "filename": "photo1.jpg", "status": "DONE"}
  ],
  "status_label": "Creating your memorial video...",
  "progress_percent": 60
}
"""

import logging
import os
from shared.db import get_order, get_order_files
from shared.models import OrderStatus, FileStatus
from shared.response import ok, not_found, server_error

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

VIDEOS_BUCKET  = os.environ.get("VIDEOS_BUCKET", "")
VIDEOS_CF_URL  = os.environ.get("VIDEOS_CF_URL", "").rstrip("/")


def _cf_video_url(raw_url: str) -> str:
    """Rewrite a direct S3 video URL to CloudFront. No-op if already CF or env vars missing."""
    if not raw_url or not VIDEOS_BUCKET or not VIDEOS_CF_URL:
        return raw_url
    s3_prefix = f"https://{VIDEOS_BUCKET}.s3.eu-west-1.amazonaws.com/"
    if raw_url.startswith(s3_prefix):
        key = raw_url[len(s3_prefix):]
        cf_url = f"{VIDEOS_CF_URL}/{key}"
        logger.info(f"Rewrote S3 URL to CloudFront: {cf_url}")
        return cf_url
    return raw_url

STATUS_LABELS = {
    OrderStatus.PENDING_UPLOAD:  "Waiting for files to upload…",
    OrderStatus.PENDING_PAYMENT: "Awaiting payment…",
    OrderStatus.PAID:            "Payment confirmed — queuing your video…",
    OrderStatus.PROCESSING:      "Creating your memorial video…",
    OrderStatus.MONTAGE:         "Assembling the final tribute video…",
    OrderStatus.COMPLETE:        "Your tribute video is ready! 🎬",
    OrderStatus.FAILED:          "Something went wrong. We'll be in touch shortly.",
}

STATUS_PROGRESS = {
    OrderStatus.PENDING_UPLOAD:  10,
    OrderStatus.PENDING_PAYMENT: 20,
    OrderStatus.PAID:            30,
    OrderStatus.PROCESSING:      60,
    OrderStatus.MONTAGE:         85,
    OrderStatus.COMPLETE:        100,
    OrderStatus.FAILED:          0,
}


def lambda_handler(event, context):
    order_id = (event.get("pathParameters") or {}).get("order_id")
    logger.info(f"GET /orders/{order_id}")

    if not order_id:
        logger.warning("Missing order_id in pathParameters")
        return not_found("Order")

    try:
        order = get_order(order_id)
    except Exception as e:
        logger.error(f"DB error fetching order {order_id}: {e}", exc_info=True)
        return server_error()

    if not order:
        logger.info(f"Order not found: {order_id}")
        return not_found("Order")

    logger.info(f"Order {order_id} found: status={order.status} amount_cents={order.total_amount_cents!r} (type={type(order.total_amount_cents).__name__}) qty={order.stone_quantity!r} (type={type(order.stone_quantity).__name__})")

    # Get file statuses
    files = []
    try:
        order_files = get_order_files(order_id)
        logger.info(f"Order {order_id} has {len(order_files)} file(s)")
        files = [
            {
                "file_id": f.file_id,
                "filename": f.original_filename,
                "status": f.status,
                "sort_order": f.sort_order,
                "caption": f.caption,
            }
            for f in order_files
        ]
    except Exception as e:
        logger.warning(f"Could not fetch files for order {order_id}: {e}", exc_info=True)

    # Calculate dynamic progress if PROCESSING
    progress = STATUS_PROGRESS.get(order.status, 0)
    if order.status == OrderStatus.PROCESSING and files:
        done_count = sum(1 for f in files if f["status"] == FileStatus.DONE)
        progress = 30 + int((done_count / len(files)) * 50)  # 30-80%

    return ok({
        "order_id": order.order_id,
        "status": order.status,
        "status_label": STATUS_LABELS.get(order.status, order.status),
        "progress_percent": progress,

        "customer_name": order.customer_name,
        "loved_one_name": order.loved_one_name,
        "loved_one_dob": order.loved_one_dob,
        "loved_one_dod": order.loved_one_dod,
        "stone_message": order.stone_message,
        "stone_style": order.stone_style,
        "stone_quantity": order.stone_quantity,
        "total_amount_euros": order.total_amount_cents / 100,

        "video_url": _cf_video_url(order.video_url),
        "tribute_page_url": order.tribute_page_url,
        "qr_code_url": order.qr_code_url,

        "files": files,
        "created_at": order.created_at,
        "completed_at": order.completed_at,
    })
