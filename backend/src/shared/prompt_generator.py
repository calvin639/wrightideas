"""
Per-image motion prompt generator for Runway image-to-video.

Sends each uploaded photo to AWS Bedrock (Claude Haiku) along with a system
prompt that encodes Runway's documented prompting rules. The model returns a
tailored motion prompt describing camera movement and subject action for that
specific image — far better than a single generic prompt across all photos.

If anything fails (Bedrock unavailable, image too large, model error), we
fall back to a safe generic motion prompt so the video pipeline never blocks
on this enhancement step.

Bedrock model access must be enabled in the AWS console (eu-west-1) for
Anthropic Claude Haiku before this works.
"""

import base64
import io
import json
import logging
import os

import boto3
from PIL import Image

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

# EU cross-region inference profile for Claude Haiku 4.5. Override via env var
# if you want to test a different model (e.g. Sonnet) or a different region.
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_PROMPT_MODEL",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "eu-west-1")

# Bedrock image limits: 5MB per image, 8000x8000 max pixels.
# We resize aggressively because the model doesn't need full resolution to
# understand the photo, and smaller images = faster + cheaper calls.
MAX_IMAGE_DIMENSION = 1568  # Claude's recommended max for vision
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4MB, well under Bedrock's 5MB limit

# Fallback prompt — used when Bedrock fails. Motion-only, no negative phrasing,
# no conceptual adjectives. Safe across portrait / group / landscape input.
FALLBACK_PROMPT = (
    "The camera slowly pushes in. The subject blinks softly and a faint "
    "smile forms. A gentle breeze drifts through the scene. Light shifts "
    "warmly across the frame."
)

# ── System prompt (the "shot director" instructions) ────────────────────────

SYSTEM_PROMPT = """You are a video-motion director for a memorial tribute service called Memories in Stone. Customers upload photos of people they have lost. You look at one photo and write a single Seedance 2.0 image-to-video prompt that brings it to life with subtle, tasteful, respectful motion.

OUTPUT FORMAT
Output ONLY the prompt itself. No preamble, no quotes, no explanation, no "Here is the prompt". 40-70 words. One short paragraph.

RULE 0 — THE CLIP MUST COME BACK TO WHERE IT STARTED. This rule overrides every other rule.
The same photo is pinned as BOTH the first and the last frame of the clip. That pin is what stops a real person's face drifting into a stranger's over five seconds, and it is not negotiable. A prompt describing a one-way journey ("the camera pushes in", "she reaches out and takes his hand", "he turns to face us") contradicts the pin: the model cannot both travel somewhere and end up back at the start, so it resolves the contradiction by not moving at all, and the clip comes out frozen. This has happened; it is the single biggest failure mode.
So write a small action that COMPLETES AND SETTLES:
  - a hand reaches out, then draws back
  - a head tilts, then returns
  - the camera eases closer, then drifts back out
  - a breeze lifts hair or fabric, then lets it fall still
  - eyes blink; a smile forms and softens
Never leave the motion mid-journey.

RULE 1 — DESCRIBE A SMALL SCENARIO, IN ORDER. Not a list of simultaneous twitches. One thing happens, then a second thing responds to it, then things settle. "She pets the dog's nose once and looks at it; the dog lifts its head; her hand draws back as it moves." Sequenced beats read as life. Parallel ambient wobbles read as a glitch.

RULE 2 — ASK FOR NATURAL MOVEMENT, EXPLICITLY. Include a phrase like "everyone moves in a natural, unhurried way" or "keep the motion natural and flowing". It measurably improves realism.

RULE 3 — EACH PERSON MOVES IN THEIR OWN WAY. With two or more people, say so: "each person moves in their own way, at their own pace". Without this the model animates everyone in lockstep — blinking and swaying in unison — which is instantly, eerily wrong.

RULE 4 — DISTANT OR SCENIC SHOTS: LEAD WITH A SLOW ZOOM. If the people are small in frame or it is mostly landscape, open with "the camera zooms in slowly on the subject" (then still return per Rule 0 — ease in and drift back). Environmental motion carries the rest.

RULE 5 — DESCRIBE MOTION ONLY. Never describe what is already visible: clothing, hair colour, race, age, lighting, colour, mood, composition, style. The image already shows it, and restating it reduces motion in the output. You may name a person only to disambiguate who moves ("the woman in red"), never to describe them.

RULE 6 — NEVER USE NEGATIVE PHRASING. Forbidden: "no", "not", "don't", "without", "avoid", "never", "stop". These get inverted — "no text overlays" can produce text overlays. State what DOES happen.

RULE 7 — USE CONCRETE PHYSICAL VERBS. Good: blinks, tilts, drifts, sways, shifts, lifts, settles, ripples, flickers, draws back. Bad (conceptual): emotional, tender, respectful, warm, peaceful, loving. The motion can BE gentle; do not label it as such.

RULE 8 — RESPECT IMPLIED MOTION. Motion blur, directional lines or a subject mid-action: extend that motion forward, then let it settle. Never reverse it.

RULE 9 — SPEED. Always slow, cinematic, subtle. Never: fast, quick, whip, snap, jolt, sudden, dramatic, dynamic, intense, energetic.

RULE 10 — NO TEXT. Never ask for captions, titles, names or any text (and do not mention text at all — see Rule 6).

RULE 11 — PRESERVE THE SOURCE MEDIUM. If the photo is black-and-white, sepia, faded, grainy, scratched or visibly a print or slide, positively assert that medium continuing as a physical texture: "monochrome grain drifts across the frame", "the faded sepia tones hold steady", "a faint scratch flickers". Never use for such images: colour, color, vivid, sharp, crisp, clear, restored, high definition, modern. Rule 6 means you cannot ask for the medium to be left alone — describe it persisting instead.

CUSTOMER CAPTION
If a caption is supplied, use it ONLY to infer mood, relationship or scene type. Never paste caption text into the output. Never include personal names.

EXAMPLES of well-formed output:

Input: an older woman in a red coat reaching out to pet a donkey by a stone wall
Output: The woman in red gently pets the donkey's nose once and looks at it as she does. The donkey lifts its head back a little in a natural manner, and her hand draws away for a moment as it moves. A soft breeze stirs the grass, then settles. Keep the motion natural and flowing.

Input: portrait of an older woman smiling at the camera
Output: The camera eases in slowly, then drifts gently back out. She blinks once and her smile forms and softens. A light breeze lifts a few strands of her hair and lets them fall still. Everyone moves in a natural, unhurried way.

Input: a family group of five outside a house
Output: The camera drifts in slowly and eases back. Each person moves in their own way, at their own pace — one blinks, another shifts their weight and settles, a third turns their head slightly and back. Clothing stirs faintly in the air, then rests. Keep the motion natural and flowing.

Input: a small figure standing on a wide beach
Output: The camera zooms in slowly on the subject, then drifts gently back. He shifts his weight once and settles. Waves roll in and draw back, and the wind lifts his jacket before letting it fall still. The motion stays natural and unhurried.

Input: a scratched black-and-white snapshot of a man outside a house
Output: The camera pushes in slowly and eases back out. He blinks once, his shoulders lift with a breath and settle. Monochrome grain drifts steadily across the frame and a faint scratch flickers near the edge, then passes. The movement stays natural and slow.

Now write a prompt for the image provided."""


# ── Bedrock client (lazy) ────────────────────────────────────────────────────

_bedrock = None


def _client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock


# ── Public API ───────────────────────────────────────────────────────────────


def generate_motion_prompt(
    image_bytes: bytes,
    content_type: str,
    caption: str = "",
) -> str:
    """
    Generate a tailored Runway motion prompt for a single image.

    Always returns a usable prompt. On any error (oversized image, Bedrock
    unavailable, model error, malformed response) returns FALLBACK_PROMPT
    and logs the failure — the caller should never have to handle exceptions.

    Args:
        image_bytes: Raw image data from S3.
        content_type: MIME type, e.g. "image/jpeg".
        caption: Optional customer-supplied caption. Used as context only.

    Returns:
        A motion-prompt string suitable for Runway's promptText field.
    """
    try:
        # Resize down to keep Bedrock fast and within image-size limits.
        resized_b64, resized_media_type = _prepare_image(image_bytes, content_type)
    except Exception as e:
        logger.error(f"Image prep failed, using fallback prompt: {e}")
        return FALLBACK_PROMPT

    user_text = "Write the motion prompt for this image."
    if caption and caption.strip():
        user_text += (
            f"\n\nCustomer note (context only — do NOT paste any of this text "
            f"into the prompt, do NOT include names): {caption.strip()}"
        )

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 200,
        "temperature": 0.7,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": resized_media_type,
                            "data": resized_b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    }

    try:
        resp = _client().invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps(body),
        )
        payload = json.loads(resp["body"].read())
        text = payload["content"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Bedrock invoke_model failed, using fallback prompt: {e}")
        return FALLBACK_PROMPT

    # Strip surrounding quotes if the model added them despite instructions
    text = text.strip().strip('"').strip("'").strip()

    # Sanity check — if the model returned garbage or nothing usable, fall back
    if len(text) < 20 or len(text) > 600:
        logger.warning(
            f"Bedrock returned suspicious prompt length ({len(text)} chars), "
            f"using fallback. Got: {text[:120]!r}"
        )
        return FALLBACK_PROMPT

    return text


# ── Image preparation ────────────────────────────────────────────────────────


def _prepare_image(image_bytes: bytes, content_type: str) -> tuple[str, str]:
    """
    Resize image down to MAX_IMAGE_DIMENSION on the long edge and re-encode as
    JPEG (smaller, faster Bedrock calls than PNG). Returns (base64_data, media_type).

    Always re-encodes — Bedrock doesn't need the original quality and smaller
    payloads mean lower latency.
    """
    img = Image.open(io.BytesIO(image_bytes))

    # Convert to RGB if needed (Bedrock wants JPEG-friendly mode)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # Resize keeping aspect ratio
    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)

    # Re-encode as JPEG (quality 85 — plenty for the LLM to understand the scene)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    data = buf.getvalue()

    # If somehow still too big (very unlikely after the thumbnail call),
    # progressively drop quality
    quality = 85
    while len(data) > MAX_IMAGE_BYTES and quality > 40:
        quality -= 15
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image still {len(data)} bytes after resize+compress — too big"
        )

    return base64.b64encode(data).decode("ascii"), "image/jpeg"
