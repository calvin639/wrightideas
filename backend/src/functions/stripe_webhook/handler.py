"""
POST /webhooks/stripe

Handles Stripe webhook events. Verifies signature to prevent forgery.

Key events handled:
  - checkout.session.completed → mark order as PAID, trigger video generation

This endpoint must receive the raw request body (not parsed) for
signature verification. API Gateway is configured to pass the raw body.
"""

import json
import os
import logging
import stripe
import boto3

from shared.db import get_order, update_order_status
from shared.models import OrderStatus
from shared.email_utils import send_order_confirmation, send_admin_new_order
from shared.response import ok, error, server_error

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
VIDEO_QUEUE_URL = os.environ.get("VIDEO_GENERATION_QUEUE_URL")

sqs = boto3.client("sqs")


def lambda_handler(event, context):
    # Stripe sends raw body — API Gateway may base64-encode it
    payload = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        payload = base64.b64decode(payload).decode("utf-8")

    sig_header = (event.get("headers") or {}).get("stripe-signature", "")

    # ── Verify Stripe signature ───────────────────────────────────────────────
    try:
        stripe_event = stripe.Webhook.construct_event(
            payload, sig_header, WEBHOOK_SECRET
        )
    except ValueError:
        logger.warning("Invalid Stripe payload")
        return error("Invalid payload", 400)
    except stripe.error.SignatureVerificationError:
        logger.warning("Invalid Stripe signature")
        return error("Invalid signature", 401)

    event_type = stripe_event["type"]
    logger.info(f"Stripe event received: {event_type}")

    # ── Handle checkout completed ─────────────────────────────────────────────
    if event_type == "checkout.session.completed":
        session = stripe_event["data"]["object"]
        _handle_payment_success(session)

    # Other events can be handled here as needed
    # e.g. payment_intent.payment_failed → notify customer

    return ok({"received": True})


def _handle_payment_success(session: dict) -> None:
    """Process a successful Stripe Checkout payment."""
    order_id = session.get("metadata", {}).get("order_id")
    payment_intent = session.get("payment_intent", "")

    if not order_id:
        logger.error("checkout.session.completed missing order_id in metadata")
        return

    logger.info(f"Payment confirmed for order {order_id} (PI: {payment_intent})")

    # Update order status to PAID
    try:
        update_order_status(
            order_id,
            OrderStatus.PAID,
            stripe_payment_intent=payment_intent or "",
        )
    except Exception as e:
        logger.error(f"Failed to update order status to PAID: {e}")
        return

    # Fetch full order for email
    try:
        from shared.db import get_order
        order = get_order(order_id)
        if order:
            send_order_confirmation(order)
            send_admin_new_order(order)
    except Exception as e:
        logger.error(f"Failed to send confirmation emails: {e}")

    # Trigger video generation pipeline
    try:
        sqs.send_message(
            QueueUrl=VIDEO_QUEUE_URL,
            MessageBody=json.dumps({
                "order_id": order_id,
                "event": "payment_confirmed",
            }),
        )
        logger.info(f"Video generation queued for order {order_id}")
    except Exception as e:
        logger.error(f"Failed to queue video generation for {order_id}: {e}")
        # TODO: alert + manual retry via DLQ
