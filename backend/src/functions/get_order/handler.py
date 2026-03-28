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
from shared.db import get_order, get_order_files
from shared.models import OrderStatus, FileStatus
from shared.response import ok, not_found, server_error

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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
    if not order_id:
        return not_found("Order")

    try:
        order = get_order(order_id)
    except Exception as e:
        logger.error(f"DB error fetching order {order_id}: {e}")
        return server_error()

    if not order:
        return not_found("Order")

    # Get file statuses
    files = []
    try:
        order_files = get_order_files(order_id)
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
        logger.warning(f"Could not fetch files for order {order_id}: {e}")

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

        "video_url": order.video_url,
        "tribute_page_url": order.tribute_page_url,
        "qr_code_url": order.qr_code_url,

        "files": files,
        "created_at": order.created_at,
        "completed_at": order.completed_at,
    })
