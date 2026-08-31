"""
Step Functions task: Failure alert (admin only)

Reached from a Catch on the pipeline's terminal stage. Emails the admin
everything needed to diagnose the order — which stage stopped it, the raw
cause from Step Functions, and the per-file status and error text — then marks
the order FAILED.

DELIBERATELY ADMIN ONLY. Most failures are fixable without the customer ever
knowing: out of provider credits, an expired presigned URL, a transient 500.
Telling someone who has just paid for a memorial video that "something went
wrong" causes alarm the fix would have made unnecessary. The customer hears
nothing until there is something real to say.

Input (from the state machine):
    {"order_id": "...", "stage": "BuildMontage", "error": {...}}
"""

import json
import logging
import os

from shared.db import get_order, get_order_files, update_order_status
from shared.email_utils import send_admin_failure_alert
from shared.models import OrderStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    order_id = event.get("order_id", "")
    stage = event.get("stage", "unknown stage")
    cause = _readable_cause(event.get("error") or {})

    logger.error("Order %s failed at %s: %s", order_id, stage, cause)

    order = get_order(order_id)
    if not order:
        logger.error("Order %s not found — cannot alert", order_id)
        return {"alerted": False, "reason": "order not found"}

    files = get_order_files(order_id)

    # Mark the order failed BEFORE emailing: if SES is down we still want the
    # state recorded, and check_pipeline.sh reads the order, not the mailbox.
    try:
        update_order_status(order_id, OrderStatus.FAILED, error_message=cause[:900])
    except Exception as e:
        logger.error("Could not set FAILED on %s: %s", order_id, e)

    try:
        sent = send_admin_failure_alert(order, stage, cause, files)
    except Exception as e:
        logger.error("Failure alert email failed for %s: %s", order_id, e, exc_info=True)
        sent = False

    return {"alerted": bool(sent), "stage": stage, "order_id": order_id}


def _readable_cause(error: dict) -> str:
    """Unwrap the Step Functions error envelope into something legible.

    A Lambda failure arrives as {"Error": "...", "Cause": "<json blob>"} where
    Cause is a JSON-encoded string holding errorMessage and a stackTrace. The
    message is the useful part; the stack goes in only if there is no message.
    """
    if not error:
        return "No error detail supplied by the state machine."
    err = error.get("Error", "")
    cause = error.get("Cause", "")
    try:
        parsed = json.loads(cause)
        msg = parsed.get("errorMessage") or ""
        typ = parsed.get("errorType") or err
        trace = "".join(parsed.get("stackTrace") or [])[:600]
        out = f"{typ}: {msg}" if msg else f"{typ}"
        return f"{out}\n\n{trace}".strip() if not msg else out
    except Exception:
        return f"{err}: {cause}"[:900] if err or cause else json.dumps(error)[:900]
