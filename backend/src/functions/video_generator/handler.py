"""
Step Functions task: Video Generator (one invocation per file)

Two actions, dispatched on `event["action"]`, so the state machine can drive a
submit -> wait -> poll loop without a second Lambda resource:

    {"action": "submit", "order_id": "...", "file_id": "..."}
        -> {"file_id": "...", "task_id": "...", "state": "RUNNING"}

    {"action": "poll", "order_id": "...", "file_id": "...", "task_id": "..."}
        -> {"file_id": "...", "state": "RUNNING"|"SUCCEEDED"|"FAILED"}

PROVIDER: fal.ai, not Runway.
Runway's API route to seedance2 hands the input to ByteDance's own safety
system, which blocks photographs of real people outright — failureCode
INPUT_PREPROCESSING.SAFETY.THIRD_PARTY, on every ordinary family photo tested,
including unprocessed camera originals. The same model on fal accepts the same
images. Runway's first-party models (gen4_turbo, gen4.5) accept faces but reject
the first/last keyframe array, so they cannot pin identity. fal is the only
route that gives us both.

KEYFRAMES — THE POINT OF THIS FILE
Identity drift happens because the model has five seconds of freedom to
reinvent a face. Seedance accepts `end_image_url`, so we pin the SAME prepared
frame as both first and last. The clip must return to the exact source pixels,
which bounds drift by construction rather than by prompt wording.

That pin only produces MOTION if the prompt describes an action that resolves
back to rest. A one-way prompt ("the camera pushes in") contradicts the pin and
the model responds by not moving at all. See RULE 0 in
`shared/prompt_generator.SYSTEM_PROMPT` — the two are a matched pair, and
changing one without the other produces frozen clips.

Kling (v3 Pro and O3 Pro) also accepts `end_image_url` but treats identical
endpoints as a hard constraint and freezes regardless of prompt — measured, do
not substitute it. Vidu Q3 works and is the viable fallback.

fal's queue API differs from Runway's task API in one way that matters: the
status/response URLs drop the endpoint sub-path (submit to
`queue.fal.run/owner/app/image-to-video`, poll at
`queue.fal.run/owner/app/requests/{id}/status`). Rather than reconstruct them,
we persist the URLs fal hands back and poll exactly those.
"""

import json
import logging
import os

import boto3
import requests

from shared.db import get_order, get_order_file, update_file_status
from shared.models import FileStatus, MediaKind
from shared.prompt_generator import generate_motion_prompt, FALLBACK_PROMPT
from shared.secrets import get_fal_key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FAL_QUEUE_BASE = "https://queue.fal.run"
FAL_MODEL = os.environ.get("FAL_MODEL", "bytedance/seedance-2.0/image-to-video")
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET")
CLIP_DURATION = int(os.environ.get("CLIP_DURATION", "5"))
CLIP_RESOLUTION = os.environ.get("CLIP_RESOLUTION", "720p")

# fal fetches the image server-side, and its queue can hold a request before
# work starts, so the presigned URL must outlive the whole queue wait — not
# just the submit call. A stale URL fails as "ImageDownloadFailure", which
# reads like a model problem and is not one.
S3_PRESIGN_EXPIRY = int(os.environ.get("S3_PRESIGN_EXPIRY", "21600"))  # 6h

# Models known to accept an end frame. Sending end_image_url to a model outside
# this set is a validation error, so the capability is declared, not assumed.
KEYFRAME_MODELS = {
    "bytedance/seedance-2.0/image-to-video",
    "fal-ai/vidu/q3/image-to-video",
}

ENABLE_KEYFRAMES = os.environ.get("ENABLE_KEYFRAMES", "true").lower() == "true"
USE_PER_IMAGE_PROMPTS = os.environ.get("USE_PER_IMAGE_PROMPTS", "true").lower() == "true"

# Seedance defaults generate_audio to TRUE, and the audio it invents is screened
# for copyright — one test run was killed outright by that check on a perfectly
# good video. The montage strips clip audio and lays its own track over the top,
# so generated audio is paid for, discarded, and a failure risk. Always off.
GENERATE_AUDIO = False

s3 = boto3.client("s3")


def lambda_handler(event, context):
    action = event.get("action", "submit")
    if action == "submit":
        return _submit(event["order_id"], event["file_id"])
    if action == "poll":
        return _poll(event["order_id"], event["file_id"], event.get("task_id", ""))
    raise ValueError(f"Unknown action: {action}")


def _supports_keyframes(model: str) -> bool:
    return ENABLE_KEYFRAMES and model in KEYFRAME_MODELS


def _fal_headers() -> dict:
    return {
        "Authorization": f"Key {get_fal_key()}",
        "Content-Type": "application/json",
    }


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
        return {"file_id": file_id, "state": "SUCCEEDED", "task_id": f.gen_request_id}

    # Per-order override exists for internal A/B testing; empty means env default.
    model = (getattr(order, "video_model", "") or "").strip() or FAL_MODEL

    # Reuse a previously generated prompt on retries — it is already paid for at
    # Bedrock and keeps a retry identical to the original attempt.
    prompt = f.motion_prompt or _build_prompt(f)

    try:
        submitted = _submit_to_fal(f, prompt, model)
    except Exception as e:
        logger.error("Submit failed for %s: %s", file_id, e, exc_info=True)
        update_file_status(
            order_id, file_id, FileStatus.FAILED,
            error_message=str(e), motion_prompt=prompt,
        )
        return {"file_id": file_id, "state": "FAILED", "task_id": "", "error": str(e)}

    update_file_status(
        order_id, file_id, FileStatus.PROCESSING,
        motion_prompt=prompt,
        gen_request_id=submitted["request_id"],
        gen_status_url=submitted["status_url"],
        gen_response_url=submitted["response_url"],
        gen_model=model,
    )
    logger.info(
        "File %s submitted to fal as %s (model=%s)",
        file_id, submitted["request_id"], model,
    )
    return {"file_id": file_id, "task_id": submitted["request_id"], "state": "RUNNING"}


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


def _payload_for(model: str, image_url: str, prompt: str) -> dict:
    """Per-model request body. Parameter names are NOT consistent across models.

    seedance and Vidu take `image_url`; Kling v3 takes `start_image_url` while
    Kling O3 takes `image_url`. Declared per model rather than assumed.
    """
    body = {"prompt": prompt, "image_url": image_url, "duration": CLIP_DURATION}

    if model.startswith("fal-ai/vidu"):
        body["resolution"] = CLIP_RESOLUTION
        body["audio"] = GENERATE_AUDIO
    else:
        body["resolution"] = CLIP_RESOLUTION
        body["generate_audio"] = GENERATE_AUDIO

    if _supports_keyframes(model):
        # The same frame at both ends. The clip must return to the source
        # pixels, which bounds drift by construction. Only works alongside a
        # resolve-to-rest prompt — see the module docstring.
        body["end_image_url"] = image_url

    return body


def _submit_to_fal(f, prompt: str, model: str) -> dict:
    image_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": UPLOADS_BUCKET, "Key": _image_key(f)},
        ExpiresIn=S3_PRESIGN_EXPIRY,
    )

    resp = requests.post(
        f"{FAL_QUEUE_BASE}/{model}",
        json=_payload_for(model, image_url, prompt),
        headers=_fal_headers(),
        timeout=60,
    )

    if resp.status_code not in (200, 201):
        detail = resp.text[:500]
        # Surface keyframe rejections explicitly, but only when the response
        # actually implicates the end frame. A blanket "the keyframes broke"
        # on every 4xx sends the reader to disable identity pinning for what
        # was really an out-of-credits or bad-parameter error.
        if "end_image_url" in detail:
            logger.error(
                "fal rejected the end keyframe for model '%s'. If this is a "
                "capability change, remove it from KEYFRAME_MODELS — but note "
                "that drops identity pinning. Response: %s",
                model, detail,
            )
        raise RuntimeError(f"fal API error {resp.status_code}: {detail}")

    data = resp.json()
    request_id = data.get("request_id")
    if not request_id:
        raise RuntimeError(f"fal returned no request_id: {resp.text[:300]}")

    # Persist the URLs fal gives us rather than rebuilding them: the queue path
    # drops the endpoint sub-path, so a reconstructed URL 404s or, worse,
    # silently returns nothing useful.
    return {
        "request_id": request_id,
        "status_url": data.get("status_url", ""),
        "response_url": data.get("response_url", ""),
    }


# ── Poll ─────────────────────────────────────────────────────────────────────


def _poll(order_id: str, file_id: str, task_id: str) -> dict:
    """Check one fal request. The state machine loops on state == RUNNING.

    The status/response URLs come from the file record rather than the state
    machine payload, so the ASL contract stays a plain task id.
    """
    f = get_order_file(order_id, file_id)
    if not f or not f.gen_status_url:
        msg = f"No fal status URL recorded for file {file_id}"
        logger.error(msg)
        update_file_status(order_id, file_id, FileStatus.FAILED, error_message=msg)
        return {"file_id": file_id, "state": "FAILED", "error": msg}

    resp = requests.get(f.gen_status_url, headers=_fal_headers(), timeout=30)
    resp.raise_for_status()
    status = resp.json().get("status")

    if status in ("IN_QUEUE", "IN_PROGRESS"):
        logger.info("Request %s still %s", task_id or f.gen_request_id, status)
        return {"file_id": file_id, "state": "RUNNING", "task_id": task_id}

    # Terminal: fetch the response payload. fal reports generation failures as a
    # 4xx on this endpoint with a `detail` body, not as a FAILED status, so a
    # non-200 here is a result to record rather than an exception to raise.
    out = requests.get(f.gen_response_url, headers=_fal_headers(), timeout=60)
    if out.status_code not in (200, 201):
        err = _describe_failure(out.text)
        update_file_status(order_id, file_id, FileStatus.FAILED, error_message=err)
        logger.error("Request %s failed: %s", task_id, err)
        return {"file_id": file_id, "state": "FAILED", "error": err}

    video_url = (out.json().get("video") or {}).get("url", "")
    if not video_url:
        msg = f"fal reported {status} with no video URL: {out.text[:300]}"
        update_file_status(order_id, file_id, FileStatus.FAILED, error_message=msg)
        return {"file_id": file_id, "state": "FAILED", "error": msg}

    from shared.clips import store_clip
    clip_key = store_clip(video_url, order_id, file_id)
    update_file_status(
        order_id, file_id, FileStatus.DONE, generated_video_s3_key=clip_key
    )
    logger.info("File %s complete: %s", file_id, clip_key)
    return {"file_id": file_id, "state": "SUCCEEDED", "clip_key": clip_key}


def _describe_failure(body: str) -> str:
    """Pull the human-readable reason out of a fal error body.

    fal nests it as {"detail": [{"msg": ...}]} or {"detail": "..."} depending on
    whether the failure came from request validation or the model itself.
    """
    try:
        detail = json.loads(body).get("detail")
    except Exception:
        return body[:300]
    if isinstance(detail, str):
        return detail[:300]
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            return str(first.get("msg") or first)[:300]
    return str(detail)[:300]
