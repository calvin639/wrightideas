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
from shared.secrets import get_stripe_key, get_stripe_webhook_secret

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
sfn = boto3.client("stepfunctions")


def lambda_handler(event, context):
    # Init Stripe with runtime secret
    stripe.api_key = get_stripe_key()
    webhook_secret = get_stripe_webhook_secret()

    # Stripe sends raw body — API Gateway may base64-encode it
    payload = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        payload = base64.b64decode(payload).decode("utf-8")

    sig_header = (event.get("headers") or {}).get("stripe-signature", "")

    # ── Verify Stripe signature ───────────────────────────────────────────────
    try:
        stripe_event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
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


def _handle_payment_success(session) -> None:
    """Process a successful Stripe Checkout payment.

    session is a stripe.checkout.Session StripeObject (not a plain dict).
    Newer Stripe SDK (v5+) uses attribute access; avoid .get() on StripeObjects.
    """
    metadata = getattr(session, "metadata", None) or {}
    # metadata may be a StripeObject or a plain dict depending on SDK version
    if isinstance(metadata, dict):
        order_id = metadata.get("order_id")
    else:
        order_id = getattr(metadata, "order_id", None)

    payment_intent = getattr(session, "payment_intent", "") or ""

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

    _start_pipeline(order_id)


def _start_pipeline(order_id: str) -> None:
    """Kick off the Step Functions execution that processes the whole order.

    The execution name is derived from the order ID, which makes this
    idempotent for free: Stripe retries webhooks, and a second StartExecution
    with the same name is rejected with ExecutionAlreadyExists rather than
    producing a duplicate pipeline run (and a duplicate customer email).
    """
    from shared.db import get_order_files

    try:
        files = get_order_files(order_id)
        if not files:
            logger.error("Order %s has no files — not starting pipeline", order_id)
            return

        sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=f"order-{order_id}",
            input=json.dumps({
                "order_id": order_id,
                "files": [{"file_id": f.file_id} for f in files],
            }),
        )
        logger.info("Pipeline started for order %s (%s files)", order_id, len(files))

    except sfn.exceptions.ExecutionAlreadyExists:
        logger.info("Pipeline already running for order %s — Stripe retry", order_id)
    except Exception as e:
        logger.error("Failed to start pipeline for %s: %s", order_id, e, exc_info=True)
        # TODO: alert + manual retry via DLQ
