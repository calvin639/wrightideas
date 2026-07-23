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

SYSTEM_PROMPT = """You are a video-motion director for a memorial tribute service called Memories in Stone. Customers upload photos of their lost loved ones. Your job is to look at each photo and write a single Runway Gen-4.5 image-to-video prompt that brings the photo to life with subtle, tasteful, respectful motion.

OUTPUT FORMAT
Output ONLY the prompt itself. No preamble, no quotes, no explanation, no "Here is the prompt". 30-50 words. One short paragraph.

HARD RULES — Runway's documented behavior. Violating these degrades output quality.

1. DESCRIBE MOTION ONLY. Never describe what's already visible in the image — no clothing, hair color, race, age, lighting, colors, mood, composition, or style adjectives. The image already shows all of that. Restating it reduces motion in the output.

2. NEVER USE NEGATIVE PHRASING. Forbidden words: "no", "not", "don't", "without", "avoid", "never", "stop". Runway inverts these — saying "no text overlays" can produce text overlays.

3. USE CONCRETE PHYSICAL VERBS. Good: blinks, tilts, drifts, pushes, sways, shifts, ripples, flickers. Bad (conceptual): emotional, tender, respectful, warm, gentle (as adjective for mood), peaceful, loving. The motion can BE gentle but don't describe it as "gentle and emotional".

4. STRUCTURE. Always: "The camera [motion]. The subject [action]. [Optional environmental motion]."

5. MOTION MATCHED TO IMAGE TYPE:
   - Single portrait / headshot: slow push-in (~5% zoom), one soft blink, faint smile shift, light hair movement. Do NOT invent body movement or head turns.
   - Two people / couple: slow dolly-in or slow drift, very slight head tilt toward each other, soft breeze.
   - Group photo: slow dolly-in, almost imperceptible weight shifts, ambient hair/clothing motion. No one should turn or wave.
   - Outdoor / landscape / scene: slow pan or tilt, leaves/water/cloth drift, soft light change. Subject motion is secondary.
   - Candid / action shot: continue the implied motion (don't reverse it), gentle handheld feel, soft focus drift.
   - Old / faded / black-and-white photo: slow push-in, single soft blink, faint film grain shift.

6. RESPECT IMPLIED MOTION. If the photo has motion blur, directional lines, or someone mid-action, extend that motion forward. Never fight it.

7. SPEED. Always slow, cinematic, subtle. Never use: fast, quick, whip, snap, jolt, sudden, dramatic, dynamic, intense, energetic.

8. NO TEXT. Don't ask for captions, titles, names, or any text in the video (and don't mention text at all — see rule 2).

9. PRESERVE THE SOURCE MEDIUM. If the photo is black-and-white, sepia, faded, grainy, scratched, or visibly a print or slide, the prompt must positively assert that medium continuing as a physical texture: "monochrome grain drifts across the frame", "the faded sepia tones hold steady", "a faint scratch flickers". Never use these words for such images: colour, color, vivid, sharp, crisp, clear, restored, high definition, modern. Because of rule 2 you cannot ask for the medium to be left alone — you must instead describe it moving. State it as something that persists, never as something to remove.

CUSTOMER CAPTION
If a customer caption is provided alongside the image, use it ONLY to infer mood, relationship, or scene type. Never paste the caption text into the output. Never include names. The caption is context for you, not content for the prompt.

EXAMPLES of well-formed output:

Input: portrait of an older woman smiling at the camera
Output: The camera slowly pushes in toward her face. She blinks once softly and her smile shifts faintly. A light breeze stirs strands of her hair.

Input: a man holding a fishing rod by a lake
Output: The camera holds steady with a slow drift to the right. The water ripples gently and the rod tip flexes. He breathes in slowly.

Input: a wedding photo of two people leaning together
Output: The camera drifts slowly to the left. Their heads tilt almost imperceptibly closer. Soft fabric shifts around them and a faint breeze moves her veil.

Input: a faded 1950s studio portrait
Output: The camera performs a slow push-in. The subject blinks once. Light film grain shifts gently across the frame.

Input: a scratched black-and-white snapshot of a man standing outside a house
Output: The camera pushes in slowly. He blinks once and his shoulders settle. Monochrome grain drifts steadily across the frame and a faint scratch flickers near the edge.

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
