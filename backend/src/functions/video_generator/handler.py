"""
Step Functions task: Video Generator (one invocation per file)

Two actions, dispatched on `event["action"]`, so the state machine can drive a
submit -> wait -> poll loop without a second Lambda resource:

    {"action": "submit", "order_id": "...", "file_id": "..."}
        -> {"file_id": "...", "task_id": "...", "state": "RUNNING"}

    {"action": "poll", "order_id": "...", "file_id": "...", "task_id": "..."}
        -> {"file_id": "...", "state": "RUNNING"|"SUCCEEDED"|"FAILED"}

WHY POLLING RATHER THAN THE WEBHOOK
Runway's dev API does not reliably deliver webhook callbacks, and `webhookUrl`
is not documented for seedance2 at all. The state machine polls inside the
execution instead, which also removes the race that existed between the webhook
and the old EventBridge poller both deciding an order was complete.

KEYFRAMES — THE POINT OF THIS FILE
Identity drift happens because the model has five seconds of freedom to reinvent
a face. seedance2 accepts `promptImage` as an array of keyframes, so we pin the
SAME prepared frame as both first and last. The clip must return to the exact
source pixels, which bounds drift by construction rather than by prompt wording.

Only models known to accept the keyframe array get it — see KEYFRAME_MODELS.
"""

import json
import logging
import os

import boto3
import requests

from shared.db import get_order, get_order_file, update_file_status
from shared.models import FileStatus, MediaKind
from shared.prompt_generator import generate_motion_prompt, FALLBACK_PROMPT
from shared.secrets import get_runway_key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUNWAY_API_BASE = "https://api.dev.runwayml.com/v1"
RUNWAY_MODEL = os.environ.get("RUNWAY_MODEL", "seedance2")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET")
CLIP_DURATION = int(os.environ.get("CLIP_DURATION", "5"))

s3 = boto3.client("s3")

# Runway needs to fetch the image from S3. The presigned URL must outlive the
# whole generation, not just the submission.
S3_PRESIGN_EXPIRY = 7200

# Landscape ratio per model — Runway validates strictly and the accepted values
# differ between families. gen3a_turbo takes 1280:768; gen4/gen4.5, the veo
# models and seedance2 take 1280:720 and reject 1280:768 with a 400.
DEFAULT_RATIO = "1280:720"
MODEL_RATIOS = {"gen3a_turbo": "1280:768"}

# Models documented to accept promptImage as [{uri, position}]. Sending the
# array to a model outside this set is a 400, so the capability is declared
# rather than assumed.
KEYFRAME_MODELS = {"seedance2"}

ENABLE_KEYFRAMES = os.environ.get("ENABLE_KEYFRAMES", "true").lower() == "true"
USE_PER_IMAGE_PROMPTS = os.environ.get("USE_PER_IMAGE_PROMPTS", "true").lower() == "true"


def lambda_handler(event, context):
    action = event.get("action", "submit")
    if action == "submit":
        return _submit(event["order_id"], event["file_id"])
    if action == "poll":
        return _poll(event["order_id"], event["file_id"], event["task_id"])
    raise ValueError(f"Unknown action: {action}")


def _ratio_for_model(model: str) -> str:
    return MODEL_RATIOS.get(model, DEFAULT_RATIO)


def _supports_keyframes(model: str) -> bool:
    return ENABLE_KEYFRAMES and model in KEYFRAME_MODELS


# ── Submit ───────────────────────────────────────────────────────────────────


def _submit(order_id: str, file_id: str) -> dict:
    order = get_order(order_id)
    f = get_order_file(order_id, file_id)
    if not order or not f:
        raise ValueError(f"Order {order_id} / file {file_id} not found")

    if f.media_kind == MediaKind.VIDEO:
        # Should never be routed here, but returning cleanly is cheaper than an
        # execution failure if the state machine's filter is ever wrong.
        logger.info("File %s is a video — nothing to generate", file_id)
        return {"file_id": file_id, "state": "SKIPPED", "task_id": ""}

    if f.status == FileStatus.DONE and f.generated_video_s3_key:
        logger.info("File %s already generated — skipping", file_id)
        return {"file_id": file_id, "state": "SUCCEEDED", "task_id": f.runway_task_id}

    # Per-order override exists for internal A/B testing; empty means env default.
    model = (getattr(order, "runway_model", "") or "").strip() or RUNWAY_MODEL

    # Reuse a previously generated prompt on retries — it is already paid for at
    # Bedrock and keeps a retry identical to the original attempt.
    prompt = f.runway_prompt or _build_prompt(f)

    try:
        task_id = _submit_to_runway(f, prompt, model)
    except Exception as e:
        logger.error("Submit failed for %s: %s", file_id, e, exc_info=True)
        update_file_status(
            order_id, file_id, FileStatus.FAILED,
            error_message=str(e), runway_prompt=prompt,
        )
        return {"file_id": file_id, "state": "FAILED", "task_id": "", "error": str(e)}

    update_file_status(
        order_id, file_id, FileStatus.PROCESSING,
        runway_task_id=task_id, runway_prompt=prompt,
    )
    logger.info("File %s submitted to Runway as task %s (model=%s)", file_id, task_id, model)
    return {"file_id": file_id, "task_id": task_id, "state": "RUNNING"}


def _image_key(f) -> str:
    """Prefer the prepared frame; fall back to the raw upload.

    Prep failing should degrade quality, not lose the photo — an unenhanced
    frame still makes a clip.
    """
    if f.prepared_s3_key:
        return f.prepared_s3_key
    logger.warning("File %s has no prepared frame — using raw upload", f.file_id)
    return f.s3_key


def _build_prompt(f) -> str:
    """Per-image motion prompt from Bedrock, with a safe generic fallback."""
    if not USE_PER_IMAGE_PROMPTS:
        return FALLBACK_PROMPT
    try:
        obj = s3.get_object(Bucket=UPLOADS_BUCKET, Key=_image_key(f))
        image_bytes = obj["Body"].read()
        content_type = obj.get("ContentType") or f.content_type or "image/jpeg"
    except Exception as e:
        logger.error("Could not fetch image for prompt generation: %s", e)
        return FALLBACK_PROMPT
    return generate_motion_prompt(
        image_bytes=image_bytes, content_type=content_type, caption=f.caption or ""
    )


def _submit_to_runway(f, prompt: str, model: str) -> str:
    image_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": UPLOADS_BUCKET, "Key": _image_key(f)},
        ExpiresIn=S3_PRESIGN_EXPIRY,
    )

    if _supports_keyframes(model):
        # The same frame at both ends. The clip is forced to end where it began,
        # so the face at t=5s is provably the face at t=0.
        prompt_image = [
            {"uri": image_url, "position": "first"},
            {"uri": image_url, "position": "last"},
        ]
    else:
        prompt_image = image_url

    payload = {
        "model": model,
        "promptImage": prompt_image,
        "promptText": prompt,
        "duration": CLIP_DURATION,
        "ratio": _ratio_for_model(model),
    }

    resp = requests.post(
        f"{RUNWAY_API_BASE}/image_to_video",
        json=payload,
        headers={
            "Authorization": f"Bearer {get_runway_key()}",
            "Content-Type": "application/json",
            "X-Runway-Version": "2024-11-06",
        },
        timeout=30,
    )

    if resp.status_code not in (200, 201):
        # Surface keyframe rejections explicitly. There is deliberately no
        # fallback to a single frame or another model: a clip that drifts is
        # worse than no clip, and silently degrading would hide the fact that
        # the keyframe contract broke.
        detail = resp.text[:500]
        if resp.status_code == 400 and _supports_keyframes(model):
            logger.error(
                "Runway rejected the keyframe payload for model '%s'. If this is a "
                "capability change, remove it from KEYFRAME_MODELS. Response: %s",
                model, detail,
            )
        raise RuntimeError(f"Runway API error {resp.status_code}: {detail}")

    task_id = resp.json().get("id")
    if not task_id:
        raise RuntimeError(f"Runway returned no task ID: {resp.text[:300]}")
    return task_id


# ── Poll ─────────────────────────────────────────────────────────────────────


def _poll(order_id: str, file_id: str, task_id: str) -> dict:
    """Check one Runway task. The state machine loops on state == RUNNING."""
    resp = requests.get(
        f"{RUNWAY_API_BASE}/tasks/{task_id}",
        headers={
            "Authorization": f"Bearer {get_runway_key()}",
            "X-Runway-Version": "2024-11-06",
        },
        timeout=30,
    )
    resp.raise_for_status()
    task = resp.json()
    status = task.get("status")

    if status == "SUCCEEDED":
        outputs = task.get("output", [])
        if not outputs:
            msg = "Runway reported SUCCEEDED with no output URL"
            update_file_status(order_id, file_id, FileStatus.FAILED, error_message=msg)
            return {"file_id": file_id, "state": "FAILED", "error": msg}

        from shared.clips import store_clip
        clip_key = store_clip(outputs[0], order_id, file_id)
        update_file_status(
            order_id, file_id, FileStatus.DONE, generated_video_s3_key=clip_key
        )
        logger.info("File %s complete: %s", file_id, clip_key)
        return {"file_id": file_id, "state": "SUCCEEDED", "clip_key": clip_key}

    if status == "FAILED":
        err = task.get("failure") or task.get("error") or "Runway task failed"
        update_file_status(order_id, file_id, FileStatus.FAILED, error_message=str(err))
        logger.error("Task %s FAILED: %s", task_id, err)
        return {"file_id": file_id, "state": "FAILED", "error": str(err)}

    logger.info("Task %s still %s", task_id, status)
    return {"file_id": file_id, "state": "RUNNING", "task_id": task_id}
