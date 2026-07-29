"""
POST /orders

Creates a new order and returns presigned S3 upload URLs for each file
the customer wants to upload. Payment happens in a separate step.

Request body:
{
  "customer_name": "Jane Murphy",
  "customer_email": "jane@example.com",
  "customer_phone": "+353871234567",     // optional
  "loved_one_name": "Michael Murphy",
  "loved_one_dob": "1945-03-12",         // optional
  "loved_one_dod": "2024-11-20",         // optional
  "stone_message": "Forever in our hearts",
  "stone_style": "black_slate",
  "stone_quantity": 1,
  "files": [                             // list of files to upload
    {"filename": "photo1.jpg", "content_type": "image/jpeg", "caption": "Christmas 2022"},
    {"filename": "photo2.jpg", "content_type": "image/jpeg", "caption": ""},
    {"filename": "video1.mp4", "content_type": "video/mp4",  "caption": "Birthday speech"}
  ]
}

Response:
{
  "order_id": "uuid",
  "files": [
    {
      "file_id": "uuid",
      "filename": "photo1.jpg",
      "upload_url": "https://s3.presigned...",  // PUT to this URL
      "s3_key": "uploads/order_id/file_id/photo1.jpg"
    }
  ],
  "next_step": "Upload files, then POST /orders/{order_id}/checkout"
}
"""

import json
import os
import logging
import boto3
from botocore.exceptions import ClientError

from shared.models import (
    Order, OrderFile, media_kind_for, MediaKind,
    IMAGE_CONTENT_TYPES, VIDEO_CONTENT_TYPES, MAX_VIDEO_SECONDS,
)
from shared.db import create_order, create_order_file
from shared.pricing import calculate_price_cents, STONE_STYLES
from shared.response import ok, created, error, server_error

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
PRESIGNED_URL_EXPIRY = 3600  # 1 hour

ALLOWED_IMAGE_TYPES = IMAGE_CONTENT_TYPES
ALLOWED_VIDEO_TYPES = VIDEO_CONTENT_TYPES
ALLOWED_TYPES = ALLOWED_IMAGE_TYPES | ALLOWED_VIDEO_TYPES
MAX_FILES = 20


def _parse_crop_rect(raw) -> dict:
    """Validate a crop rectangle from the upload UI.

    Expected as fractions of the source image so it stays correct no matter what
    resolution the browser previewed at. Anything malformed returns {} rather
    than erroring — losing a crop means automatic framing is used, which is a
    far better outcome than rejecting a paid order over a bad drag gesture.
    """
    if not isinstance(raw, dict):
        return {}
    try:
        rect = {k: float(raw[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        return {}

    if rect["w"] <= 0 or rect["h"] <= 0:
        return {}
    if not all(0.0 <= rect[k] <= 1.0 for k in ("x", "y")):
        return {}
    if rect["x"] + rect["w"] > 1.001 or rect["y"] + rect["h"] > 1.001:
        return {}
    return rect


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error("Invalid JSON body")

    # ── Validate required fields ──────────────────────────────────────────────
    required = ["customer_name", "customer_email", "loved_one_name", "stone_message", "files"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return error(f"Missing required fields: {', '.join(missing)}")

    files_input = body.get("files", [])
    if not files_input:
        return error("At least one file is required")
    if len(files_input) > MAX_FILES:
        return error(f"Maximum {MAX_FILES} files allowed per order")

    # Validate file types
    for f in files_input:
        ct = f.get("content_type", "")
        if ct not in ALLOWED_TYPES:
            return error(
                f"File type '{ct}' is not allowed. "
                f"Accepted: JPEG, PNG, WebP, HEIC, MP4, MOV"
            )

    # ── Validate stone details ────────────────────────────────────────────────
    stone_style = body.get("stone_style", "black_slate")
    if stone_style not in STONE_STYLES:
        return error(f"Unknown stone style: {stone_style}")
    if not STONE_STYLES[stone_style]["available"]:
        return error(f"Stone style '{stone_style}' is not yet available")

    quantity = body.get("stone_quantity", 1)
    if not isinstance(quantity, int) or quantity < 1 or quantity > 100:
        return error("stone_quantity must be an integer between 1 and 100")

    VALID_MUSIC = {"beautiful", "emotion", "nature", "none"}
    music_choice = body.get("music_choice", "")
    if music_choice not in VALID_MUSIC:
        return error(f"music_choice must be one of: {', '.join(sorted(VALID_MUSIC))}")

    # ── Create order ──────────────────────────────────────────────────────────
    order = Order(
        customer_name=body["customer_name"].strip(),
        customer_email=body["customer_email"].strip().lower(),
        customer_phone=body.get("customer_phone", "").strip(),
        loved_one_name=body["loved_one_name"].strip(),
        loved_one_dob=body.get("loved_one_dob", ""),
        loved_one_dod=body.get("loved_one_dod", ""),
        stone_message=body["stone_message"].strip(),
        stone_style=stone_style,
        stone_quantity=quantity,
        total_amount_cents=calculate_price_cents(quantity),
        music_choice=music_choice,
        # Optional per-order model override for internal testing. Passed through
        # as-is (no allowlist) so new Runway models can be tested without a code
        # change; an invalid string just makes Runway reject the submission.
        # Empty → video_generator falls back to the RUNWAY_MODEL env default.
        runway_model=body.get("runway_model", "").strip(),
    )
    create_order(order)
    logger.info(f"Order created: {order.order_id}")

    # ── Create file records + presigned upload URLs ───────────────────────────
    file_responses = []
    for idx, f_input in enumerate(files_input):
        filename = f_input.get("filename", f"file_{idx}")
        content_type = f_input["content_type"]
        caption = f_input.get("caption", "").strip()
        kind = media_kind_for(content_type)

        # Trimming only applies to videos, and only the start point is chosen —
        # the length is fixed at MAX_VIDEO_SECONDS so one home video cannot
        # dominate the montage.
        trim_start = 0.0
        if kind == MediaKind.VIDEO:
            try:
                trim_start = max(0.0, float(f_input.get("trim_start_seconds", 0) or 0))
            except (TypeError, ValueError):
                trim_start = 0.0

        order_file = OrderFile(
            order_id=order.order_id,
            original_filename=filename,
            content_type=content_type,
            media_kind=kind,
            caption=caption,
            sort_order=idx,
            crop_rect=_parse_crop_rect(f_input.get("crop_rect")),
            trim_start_seconds=trim_start,
            source_duration_seconds=float(f_input.get("duration_seconds", 0) or 0),
            s3_key=f"uploads/{order.order_id}/{idx:02d}_{filename}",
        )
        create_order_file(order_file)

        # Generate presigned PUT URL
        try:
            presigned_url = s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": UPLOADS_BUCKET,
                    "Key": order_file.s3_key,
                    "ContentType": content_type,
                },
                ExpiresIn=PRESIGNED_URL_EXPIRY,
            )
        except ClientError as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            return server_error("Failed to generate upload URLs")

        file_responses.append({
            "file_id": order_file.file_id,
            "filename": filename,
            "s3_key": order_file.s3_key,
            "upload_url": presigned_url,
            "sort_order": idx,
        })

    return created({
        "order_id": order.order_id,
        "total_amount_euros": order.total_amount_cents / 100,
        "stone_quantity": quantity,
        "files": file_responses,
        "presigned_url_expires_in_seconds": PRESIGNED_URL_EXPIRY,
        "next_step": f"1. PUT each file to its upload_url  2. POST /orders/{order.order_id}/checkout",
    })
