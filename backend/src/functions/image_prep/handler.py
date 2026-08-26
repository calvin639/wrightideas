"""
Step Functions task: Image Prep (one invocation per file)

Downloads one uploaded file, prepares it for Runway, and writes the result back
to S3. Invoked from the PrepareFiles Map state, so a 20-photo order runs as 20
short invocations instead of one that blows the Lambda timeout.

This is the only container-image function in the stack — it carries torch for
Real-ESRGAN and GFPGAN. See the Dockerfile.

Input (from the Map state):
    {"order_id": "uuid", "file_id": "uuid"}

Output:
    {"file_id": "uuid", "status": "PREPARED"|"SKIPPED"|"FAILED",
     "media_kind": "IMAGE"|"VIDEO", "prepared_key": "..."}

Failures are returned, not raised. One unreadable photo should cost the customer
that photo, not the order — the state machine reads `status` and carries on.
"""

import logging
import os

import boto3

from shared import image_prep, image_restore
from shared.db import get_order_file, update_file_status
from shared.models import FileStatus, MediaKind

logger = logging.getLogger()
logger.setLevel(logging.INFO)

UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")

# Keeping the untouched original next to the prepared frame makes every
# enhancement decision auditable against the source. Cheap, and the only way to
# tell whether restoration helped or hurt on a real customer photo.
KEEP_SOURCE_COPY = os.environ.get("KEEP_SOURCE_COPY", "true").lower() == "true"

s3 = boto3.client("s3")


def lambda_handler(event, context):
    order_id = event["order_id"]
    file_id = event["file_id"]

    try:
        return _prepare_file(order_id, file_id)
    except Exception as e:
        logger.error("Prep failed for %s/%s: %s", order_id, file_id, e, exc_info=True)
        update_file_status(order_id, file_id, FileStatus.FAILED, error_message=str(e))
        return {"file_id": file_id, "status": FileStatus.FAILED.value, "error": str(e)}


def _prepare_file(order_id: str, file_id: str) -> dict:
    f = get_order_file(order_id, file_id)
    if not f:
        raise ValueError(f"File {file_id} not found on order {order_id}")

    # Videos have nothing to prepare — no frame to enhance and nothing for
    # Runway to animate. They are trimmed by the montage builder instead.
    if f.media_kind == MediaKind.VIDEO:
        logger.info("File %s is a video — skipping prep and Runway", file_id)
        update_file_status(order_id, file_id, FileStatus.SKIPPED)
        return {
            "file_id": file_id,
            "status": FileStatus.SKIPPED.value,
            "media_kind": MediaKind.VIDEO.value,
            "prepared_key": "",
        }

    if f.status in (FileStatus.PREPARED, FileStatus.PROCESSING, FileStatus.DONE):
        # Idempotent on state machine retries — never redo an expensive
        # restoration that already succeeded.
        logger.info("File %s already %s — skipping prep", file_id, f.status)
        return {
            "file_id": file_id,
            "status": FileStatus.PREPARED.value,
            "media_kind": MediaKind.IMAGE.value,
            "prepared_key": f.prepared_s3_key,
        }

    update_file_status(order_id, file_id, FileStatus.PREPARING)

    raw = s3.get_object(Bucket=UPLOADS_BUCKET, Key=f.s3_key)["Body"].read()
    logger.info("Preparing %s (%s bytes) crop=%s", f.s3_key, len(raw), bool(f.crop_rect))

    # Detect faces ONCE on the oriented source; the same boxes drive the
    # face-aware framing of the enhanced frame, the identically-framed _before
    # control, and (transformed) the adaptive restore weight. RetinaFace is
    # the most expensive model in this function — one pass, one truth.
    img = image_prep.load_and_orient(raw)
    assessment = image_prep.assess(img)
    faces = image_restore.detect_faces(img)   # None = detector unavailable

    # `restore` is injected rather than imported inside image_prep, so the
    # deterministic module stays torch-free. If the torch stack failed to load,
    # image_restore.restore returns the image untouched and prep still succeeds.
    out, restore_meta = image_prep.prepare(
        img, assessment,
        crop_rect=f.crop_rect or None,
        restore=image_restore.restore,
        faces=faces,
    )
    jpeg = image_prep.to_jpeg_bytes(out)

    prepared_key = f"prepared/{order_id}/{file_id}.jpg"
    s3.put_object(
        Bucket=UPLOADS_BUCKET,
        Key=prepared_key,
        Body=jpeg,
        ContentType="image/jpeg",
    )

    if KEEP_SOURCE_COPY:
        _store_source_reference(
            img, assessment, faces, f.crop_rect or None, order_id, file_id
        )

    update_file_status(
        order_id, file_id,
        FileStatus.PREPARED,
        prepared_s3_key=prepared_key,
        assessment=assessment.to_dict(),
        restore_meta=restore_meta,
    )

    logger.info(
        "Prepared %s -> %s (tier=%s restored=%s %sx%s)",
        file_id, prepared_key, assessment.tier, restore_meta.get("restored"),
        assessment.width, assessment.height,
    )
    return {
        "file_id": file_id,
        "status": FileStatus.PREPARED.value,
        "media_kind": MediaKind.IMAGE.value,
        "prepared_key": prepared_key,
    }


def _store_source_reference(
    img, assessment, faces, crop_rect, order_id: str, file_id: str
) -> None:
    """Write the framed-but-unenhanced version alongside the prepared frame.

    Not the raw upload — that is already in S3 untouched. This is the same
    framing and resize with restoration and tone correction switched off, which
    makes it a like-for-like control for judging whether enhancement helped.
    The SAME face boxes are passed so both frames get the identical face-aware
    crop — a control framed differently from the frame it controls for is no
    control at all.
    """
    try:
        before, _ = image_prep.prepare(
            img, assessment, crop_rect=crop_rect,
            tone_strength=0.0, sharpen=False, faces=faces,
        )
        s3.put_object(
            Bucket=UPLOADS_BUCKET,
            Key=f"prepared/{order_id}/{file_id}_before.jpg",
            Body=image_prep.to_jpeg_bytes(before),
            ContentType="image/jpeg",
        )
    except Exception as e:
        # Purely diagnostic — never fail an order over it.
        logger.warning("Could not store before-image for %s: %s", file_id, e)
