"""
POST /orders/{order_id}/checkout

Creates a Stripe Checkout session for an existing order.
Frontend redirects customer to Stripe's hosted checkout page.

Response:
{
  "checkout_url": "https://checkout.stripe.com/...",
  "session_id": "cs_..."
}
"""

import json
import os
import logging
import stripe

from shared.db import get_order, set_order_stripe_session
from shared.models import OrderStatus
from shared.pricing import get_line_item_description
from shared.response import ok, error, not_found, server_error

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://memories.wrightideas.co")


def lambda_handler(event, context):
    order_id = (event.get("pathParameters") or {}).get("order_id")
    if not order_id:
        return not_found("Order")

    # Fetch the order
    try:
        order = get_order(order_id)
    except Exception as e:
        logger.error(f"DB error: {e}")
        return server_error()

    if not order:
        return not_found("Order")

    # Guard: don't create duplicate sessions for already-paid orders
    if order.status in (OrderStatus.PAID, OrderStatus.PROCESSING,
                        OrderStatus.MONTAGE, OrderStatus.COMPLETE):
        return error("Order has already been paid", 409)

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            currency="eur",
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": order.total_amount_cents,
                    "product_data": {
                        "name": get_line_item_description(order.stone_quantity),
                        "description": (
                            f"Memorial stone for {order.loved_one_name}. "
                            f"Includes personalised AI tribute video and QR code."
                        ),
                        "images": [
                            f"{FRONTEND_URL}/images/stone-product.jpg"
                        ],
                    },
                },
                "quantity": 1,
            }],
            metadata={
                "order_id": order_id,
                "loved_one_name": order.loved_one_name,
            },
            customer_email=order.customer_email,
            success_url=f"{FRONTEND_URL}/order/success?order_id={order_id}",
            cancel_url=f"{FRONTEND_URL}/order/cancelled?order_id={order_id}",
            payment_intent_data={
                "metadata": {
                    "order_id": order_id,
                }
            },
        )
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating session: {e}")
        return server_error("Failed to create payment session")

    # Store session ID on order
    try:
        set_order_stripe_session(
            order_id=order_id,
            session_id=session.id,
            amount_cents=order.total_amount_cents,
        )
    except Exception as e:
        logger.error(f"Failed to save stripe session to DB: {e}")
        # Don't block the response — customer can still pay

    logger.info(f"Stripe session created for order {order_id}: {session.id}")
    return ok({
        "checkout_url": session.url,
        "session_id": session.id,
    })
