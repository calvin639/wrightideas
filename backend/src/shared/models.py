"""
DynamoDB data models for Memories in Stone.

Single-table design:

  Order record:
    PK  = ORDER#{order_id}
    SK  = METADATA
    GSI1PK = STATUS#{status}
    GSI1SK = {created_at}

  File record:
    PK  = ORDER#{order_id}
    SK  = FILE#{file_id}
    GSI1PK = RUNWAY#{runway_task_id}   (set after Runway job submitted)
    GSI1SK = {created_at}
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional
import uuid


def _coerce(v):
    """Convert DynamoDB Decimal to int or float so json.dumps doesn't choke.

    Recurses into maps and lists: crop rectangles and image assessments are
    stored as nested maps of floats, and DynamoDB hands every one of them back
    as a Decimal. Without the recursion those nested values reach json.dumps
    untouched and raise.
    """
    if isinstance(v, Decimal):
        return int(v) if v % 1 == 0 else float(v)
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_coerce(x) for x in v]
    return v


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


# ── ORDER STATUS ──────────────────────────────────────────────────────────────

class OrderStatus(str, Enum):
    PENDING_UPLOAD   = "PENDING_UPLOAD"    # Order created, awaiting file uploads
    PENDING_PAYMENT  = "PENDING_PAYMENT"   # Files uploaded, awaiting Stripe payment
    PAID             = "PAID"              # Payment confirmed, processing queued
    PROCESSING       = "PROCESSING"        # AI video generation in progress
    MONTAGE          = "MONTAGE"           # All clips done, building final video
    COMPLETE         = "COMPLETE"          # Final video ready, email sent
    FAILED           = "FAILED"            # Something went wrong


# ── FILE STATUS ───────────────────────────────────────────────────────────────

class FileStatus(str, Enum):
    UPLOADED   = "UPLOADED"     # Raw file in S3
    PREPARING  = "PREPARING"    # Being oriented / cropped / enhanced
    PREPARED   = "PREPARED"     # Enhanced frame ready for Runway
    PROCESSING = "PROCESSING"   # Submitted to Runway ML
    DONE       = "DONE"         # Clip generated successfully
    SKIPPED    = "SKIPPED"      # Customer video — bypasses Runway, montage uses it directly
    FAILED     = "FAILED"       # Runway job failed


# ── MEDIA KIND ────────────────────────────────────────────────────────────────

class MediaKind(str, Enum):
    """What kind of upload this is, which decides its route through the pipeline.

    Images go through prep and Runway. Videos skip both — there is nothing to
    animate — and are trimmed straight into the montage.
    """
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"


# Customer video clips are capped to match the length of a generated clip, so
# one long home video cannot dominate the montage.
MAX_VIDEO_SECONDS = 5.0

IMAGE_CONTENT_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif",
}
VIDEO_CONTENT_TYPES = {"video/mp4", "video/quicktime", "video/mov"}


def media_kind_for(content_type: str) -> str:
    """Route an upload by MIME type. Unknown types are treated as images —
    create_order validates against the allow-lists before this is reached."""
    return MediaKind.VIDEO if content_type in VIDEO_CONTENT_TYPES else MediaKind.IMAGE


# ── ORDER ─────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    order_id: str = field(default_factory=new_id)
    status: str = OrderStatus.PENDING_UPLOAD

    # Customer details
    customer_name: str = ""
    customer_email: str = ""
    customer_phone: str = ""

    # Loved one details
    loved_one_name: str = ""
    loved_one_dob: str = ""    # ISO date string e.g. "1940-05-12"
    loved_one_dod: str = ""    # ISO date string e.g. "2024-11-30"
    stone_message: str = ""    # Engraved message

    # Stone details
    stone_style: str = "black_slate"
    stone_quantity: int = 1
    total_amount_cents: int = 0
    music_choice: str = ""       # beautiful | emotion | nature | none

    # Generation override (optional — for internal A/B testing of Runway models).
    # Empty string means "use the RUNWAY_MODEL Lambda env default". Not exposed
    # in the customer order UI; set only via the test script / API for testing.
    runway_model: str = ""       # e.g. gen4.5 | seedance2

    # Stripe
    stripe_session_id: str = ""
    stripe_payment_intent: str = ""

    # Output
    video_url: str = ""
    video_s3_key: str = ""
    qr_code_url: str = ""
    qr_code_s3_key: str = ""
    qr_svg_url: str = ""
    qr_svg_s3_key: str = ""
    tribute_page_url: str = ""

    # Human-in-the-loop review of prepared frames (before Runway spend).
    # review_key is the unguessable secret embedded in the admin's email links;
    # review_task_token is the Step Functions callback token the decision
    # endpoint redeems. review_status: "" | PENDING | APPROVED | AUTO_APPROVED.
    review_key: str = ""
    review_task_token: str = ""
    review_status: str = ""

    # Meta
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str = ""
    error_message: str = ""

    def to_dynamo(self) -> dict:
        d = asdict(self)
        return {
            "PK": f"ORDER#{self.order_id}",
            "SK": "METADATA",
            "GSI1PK": f"STATUS#{self.status}",
            "GSI1SK": self.created_at,
            **d,
        }

    @classmethod
    def from_dynamo(cls, item: dict) -> "Order":
        skip = {"PK", "SK", "GSI1PK", "GSI1SK"}
        data = {k: _coerce(v) for k, v in item.items() if k not in skip}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ── FILE ──────────────────────────────────────────────────────────────────────

@dataclass
class OrderFile:
    file_id: str = field(default_factory=new_id)
    order_id: str = ""
    status: str = FileStatus.UPLOADED

    # Upload
    original_filename: str = ""
    content_type: str = ""       # e.g. "image/jpeg"
    media_kind: str = MediaKind.IMAGE
    s3_key: str = ""             # Key in uploads bucket — always the untouched original
    file_size_bytes: int = 0
    caption: str = ""            # Customer-provided caption (used as Runway prompt)
    sort_order: int = 0          # Order in the montage

    # Framing chosen in the upload UI, as fractions of the source image:
    # {"x": 0.1, "y": 0.0, "w": 0.8, "h": 0.45}. Stored as a rectangle rather
    # than a pre-cropped file so the full-resolution original stays available
    # for enhancement and the crop can be revised without a re-upload.
    crop_rect: dict = field(default_factory=dict)

    # Video trimming. Customer videos are capped at MAX_VIDEO_SECONDS; this is
    # the offset the customer chose as the start of that window.
    trim_start_seconds: float = 0.0
    source_duration_seconds: float = 0.0

    # Image prep output
    prepared_s3_key: str = ""    # 1280x720 frame actually submitted to Runway
    assessment: dict = field(default_factory=dict)   # ImageAssessment.to_dict()
    restore_meta: dict = field(default_factory=dict) # what restoration ran, if any

    # Runway ML
    runway_task_id: str = ""
    runway_prompt: str = ""

    # Output
    generated_video_s3_key: str = ""

    # Meta
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    error_message: str = ""

    def to_dynamo(self) -> dict:
        d = asdict(self)
        gsi1pk = f"RUNWAY#{self.runway_task_id}" if self.runway_task_id else f"ORDER_FILE#{self.order_id}"
        return {
            "PK": f"ORDER#{self.order_id}",
            "SK": f"FILE#{self.file_id}",
            "GSI1PK": gsi1pk,
            "GSI1SK": self.created_at,
            **d,
        }

    @classmethod
    def from_dynamo(cls, item: dict) -> "OrderFile":
        skip = {"PK", "SK", "GSI1PK", "GSI1SK"}
        data = {k: _coerce(v) for k, v in item.items() if k not in skip}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
