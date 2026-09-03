"""
DynamoDB helper functions for Memories in Stone.
"""

import os
import boto3
from boto3.dynamodb.conditions import Key
from decimal import Decimal
from typing import Optional, List
from botocore.exceptions import ClientError

from shared.models import (
    Order, OrderFile, OrderStatus, FileStatus, now_iso,
    AWAITING_PAYMENT_STATUSES,
)

_dynamodb = None


def _decimalise(v):
    """Recursively convert floats to Decimal for DynamoDB.

    The boto3 resource layer refuses Python floats outright ("Float types are
    not supported"). Crop rectangles and image assessments are nested maps full
    of them, so the conversion has to recurse rather than just check the top
    level. `models._coerce` performs the inverse on read.
    """
    if isinstance(v, bool):
        return v                      # bool is a subclass of int — leave it alone
    if isinstance(v, float):
        return Decimal(str(v))        # str() avoids binary float noise
    if isinstance(v, dict):
        return {k: _decimalise(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_decimalise(x) for x in v]
    return v


def get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"])
    return _dynamodb


# ── ORDER OPERATIONS ──────────────────────────────────────────────────────────

def create_order(order: Order) -> Order:
    """Persist a new order to DynamoDB."""
    get_table().put_item(Item=_decimalise(order.to_dynamo()))
    return order


def get_order(order_id: str) -> Optional[Order]:
    """Fetch an order by ID. Returns None if not found."""
    resp = get_table().get_item(
        Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"}
    )
    item = resp.get("Item")
    return Order.from_dynamo(item) if item else None


def update_order_status(order_id: str, status: str, **extra_fields) -> None:
    """Update order status and any extra fields atomically."""
    update_expr = "SET #st = :status, updated_at = :ts, GSI1PK = :gsi1pk"
    expr_attrs = {
        ":status": status,
        ":ts": now_iso(),
        ":gsi1pk": f"STATUS#{status}",
    }
    attr_names = {"#st": "status"}

    for key, value in extra_fields.items():
        update_expr += f", {key} = :{key}"
        expr_attrs[f":{key}"] = _decimalise(value)

    get_table().update_item(
        Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=expr_attrs,
    )


def mark_order_paid(order_id: str, stripe_payment_intent: str = "") -> bool:
    """Move an order to PAID, exactly once.

    Returns True if this call performed the transition, False if the order had
    already moved past awaiting-payment.

    The condition is the whole point. Stripe redelivers webhooks — on its own
    retry schedule, and again whenever an endpoint is re-pointed or a event is
    resent by hand — and an unconditional write would rewind a COMPLETE order
    to PAID, re-send the customer their payment confirmation, and leave the
    tracking page claiming a finished tribute was still queued. Callers send
    email only when this returns True.
    """
    try:
        get_table().update_item(
            Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
            UpdateExpression=(
                "SET #st = :paid, updated_at = :ts, GSI1PK = :gsi1pk, "
                "stripe_payment_intent = :pi"
            ),
            ConditionExpression="attribute_exists(PK) AND #st IN (:pu, :pp)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":paid": OrderStatus.PAID.value,
                ":ts": now_iso(),
                ":gsi1pk": f"STATUS#{OrderStatus.PAID.value}",
                ":pi": stripe_payment_intent or "",
                ":pu": AWAITING_PAYMENT_STATUSES[0],
                ":pp": AWAITING_PAYMENT_STATUSES[1],
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def get_orders_by_status(status: str) -> List[Order]:
    """Find all orders with a given status via GSI1."""
    resp = get_table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"STATUS#{status}")
    )
    return [Order.from_dynamo(item) for item in resp.get("Items", [])]


def get_order_by_stripe_session(stripe_session_id: str) -> Optional[Order]:
    """Find order via GSI by Stripe session ID (stored in GSI1 when set)."""
    # We scan for this — in production with high volume, add a dedicated GSI
    resp = get_table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"STRIPE#{stripe_session_id}")
    )
    items = resp.get("Items", [])
    return Order.from_dynamo(items[0]) if items else None


def set_order_stripe_session(order_id: str, session_id: str, amount_cents: int) -> None:
    """Store Stripe session ID on the order and flip GSI1PK for lookup."""
    get_table().update_item(
        Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
        UpdateExpression=(
            "SET stripe_session_id = :sid, "
            "total_amount_cents = :amt, "
            "#st = :status, "
            "GSI1PK = :gsi1pk, "
            "updated_at = :ts"
        ),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":sid": session_id,
            ":amt": amount_cents,
            ":status": OrderStatus.PENDING_PAYMENT,
            ":gsi1pk": f"STRIPE#{session_id}",
            ":ts": now_iso(),
        },
    )


# ── FILE OPERATIONS ───────────────────────────────────────────────────────────

def create_order_file(file: OrderFile) -> OrderFile:
    """Persist a new file record."""
    get_table().put_item(Item=_decimalise(file.to_dynamo()))
    return file


def get_order_file(order_id: str, file_id: str) -> Optional[OrderFile]:
    """Fetch a single file record. Used by the per-file prep and generate tasks,
    which are handed only IDs by the state machine."""
    resp = get_table().get_item(
        Key={"PK": f"ORDER#{order_id}", "SK": f"FILE#{file_id}"}
    )
    item = resp.get("Item")
    return OrderFile.from_dynamo(item) if item else None


def get_order_files(order_id: str) -> List[OrderFile]:
    """Get all files for an order, sorted by sort_order."""
    resp = get_table().query(
        KeyConditionExpression=(
            Key("PK").eq(f"ORDER#{order_id}") &
            Key("SK").begins_with("FILE#")
        )
    )
    files = [OrderFile.from_dynamo(item) for item in resp.get("Items", [])]
    return sorted(files, key=lambda f: f.sort_order)


def update_file_status(
    order_id: str,
    file_id: str,
    status: str,
    **extra_fields,
) -> None:
    """Update a file's processing status."""
    update_expr = "SET #st = :status, updated_at = :ts"
    expr_attrs = {":status": status, ":ts": now_iso()}
    attr_names = {"#st": "status"}

    for key, value in extra_fields.items():
        update_expr += f", {key} = :{key}"
        expr_attrs[f":{key}"] = _decimalise(value)

    # Keep GSI1 in sync when a Runway task ID is assigned
    # so get_file_by_runway_task() can look up files by task ID
    if "runway_task_id" in extra_fields and extra_fields["runway_task_id"]:
        task_id = extra_fields["runway_task_id"]
        update_expr += ", GSI1PK = :gsi1pk, GSI1SK = :gsi1sk"
        expr_attrs[":gsi1pk"] = f"RUNWAY#{task_id}"
        expr_attrs[":gsi1sk"] = f"FILE#{file_id}"

    get_table().update_item(
        Key={"PK": f"ORDER#{order_id}", "SK": f"FILE#{file_id}"},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=expr_attrs,
    )


def get_file_by_runway_task(runway_task_id: str) -> Optional[OrderFile]:
    """Look up a file record by its Runway task ID via GSI1."""
    resp = get_table().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"RUNWAY#{runway_task_id}")
    )
    items = resp.get("Items", [])
    return OrderFile.from_dynamo(items[0]) if items else None


# all_files_complete() and any_file_failed() were removed along with the SQS
# pipeline. They existed so two different Lambdas could each decide whether an
# order was finished; the Step Functions Map state is now that decision, and it
# cannot race with itself.
