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
from enum import Enum
from typing import Optional
import uuid


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
    PROCESSING = "PROCESSING"   # Submitted to Runway ML
    DONE       = "DONE"         # Clip generated successfully
    FAILED     = "FAILED"       # Runway job failed


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
        # Strip DynamoDB keys
        skip = {"PK", "SK", "GSI1PK", "GSI1SK"}
        data = {k: v for k, v in item.items() if k not in skip}
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
    s3_key: str = ""             # Key in uploads bucket
    file_size_bytes: int = 0
    caption: str = ""            # Customer-provided caption (used as Runway prompt)
    sort_order: int = 0          # Order in the montage

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
        data = {k: v for k, v in item.items() if k not in skip}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
