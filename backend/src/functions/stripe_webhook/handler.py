"""
POST /webhooks/stripe

Handles Stripe webhook events. Verifies signature to prevent forgery.

Key events handled:
  - checkout.session.completed        → start the pipeline IF the money settled
  - checkout.session.async_payment_succeeded → delayed method finally settled
  - checkout.session.async_payment_failed    → delayed method failed; do not build

Why three events and not one: for delayed-settlement methods (bank debits,
some wallets, vouchers) Stripe fires `checkout.session.completed` as soon as
the customer finishes the form, with `payment_status: "unpaid"`. The funds can
still fail days later. Starting image prep and paid clip generation on that
event means spending real money on an order that may never be paid for.

This endpoint must receive the raw request body (not parsed) for
signature verification. API Gateway is configured to pass the raw body.
"""

import json
import os
import logging
import stripe
import boto3

from shared.db import get_order, update_order_status, mark_order_paid
from shared.models import OrderStatus, SETTLED_PAYMENT_STATUSES
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

    # ── Route the event ───────────────────────────────────────────────────────
    if event_type in ("checkout.session.completed",
                      "checkout.session.async_payment_succeeded"):
        session = stripe_event["data"]["object"]
        status = _field(session, "payment_status") or ""
        if status in SETTLED_PAYMENT_STATUSES:
            _handle_payment_success(session)
        else:
            # Money not ours yet. Leave the order awaiting payment and wait for
            # async_payment_succeeded — Stripe will send it, or send
            # async_payment_failed instead.
            logger.info("Order %s: %s with payment_status=%r — holding, no spend",
                        _order_id(session), event_type, status)
    elif event_type == "checkout.session.async_payment_failed":
        _handle_payment_failed(session=stripe_event["data"]["object"])
    else:
        logger.info("Ignoring Stripe event %s", event_type)

    return ok({"received": True})


def _field(session, name, default=None):
    """Read a field from a Stripe object.

    session is a stripe.checkout.Session StripeObject, not a plain dict. Newer
    SDKs (v5+) use attribute access and older ones behave like dicts, so try
    both rather than pinning behaviour to an SDK version.
    """
    if isinstance(session, dict):
        return session.get(name, default)
    return getattr(session, name, default)


def _order_id(session):
    metadata = _field(session, "metadata") or {}
    if isinstance(metadata, dict):
        return metadata.get("order_id")
    return getattr(metadata, "order_id", None)


def _handle_payment_failed(session) -> None:
    """A delayed payment did not settle. Never build; leave it recoverable.

    The order is put back to PENDING_PAYMENT rather than FAILED so the customer
    can pay again against the same order and the same uploads.
    """
    order_id = _order_id(session)
    if not order_id:
        logger.error("async_payment_failed missing order_id in metadata")
        return
    logger.error("Payment FAILED to settle for order %s — pipeline not started", order_id)
    try:
        update_order_status(
            order_id, OrderStatus.PENDING_PAYMENT,
            error_message="Stripe reported the payment failed to settle.",
        )
    except Exception as e:                       # noqa: BLE001
        logger.error("Could not record payment failure for %s: %s", order_id, e)


def _handle_payment_success(session) -> None:
    """Process a successful Stripe Checkout payment.

    session is a stripe.checkout.Session StripeObject (not a plain dict).
    Newer Stripe SDK (v5+) uses attribute access; avoid .get() on StripeObjects.
    """
    order_id = _order_id(session)
    payment_intent = _field(session, "payment_intent", "") or ""

    if not order_id:
        logger.error("Settled checkout session missing order_id in metadata")
        return

    logger.info("Payment settled for order %s (PI: %s)", order_id, payment_intent)

    # Conditional: True only if this call actually moved the order out of
    # awaiting-payment. A Stripe replay of an event we already processed
    # returns False, and everything below it is skipped — no duplicate
    # confirmation email, no status rewind on an order that has since finished.
    try:
        first_time = mark_order_paid(order_id, stripe_payment_intent=payment_intent)
    except Exception as e:                       # noqa: BLE001
        logger.error("Failed to update order status to PAID: %s", e)
        return

    if not first_time:
        logger.info("Order %s already past awaiting-payment — replay, nothing to do",
                    order_id)
        # Still call _start_pipeline: it is itself idempotent, and this covers
        # the narrow window where the status write landed but StartExecution
        # did not.
        _start_pipeline(order_id)
        return

    try:
        order = get_order(order_id)
        if order:
            send_order_confirmation(order)
            send_admin_new_order(order)
    except Exception as e:                       # noqa: BLE001
        logger.error("Failed to send confirmation emails: %s", e)

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
