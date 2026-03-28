"""
DynamoDB helper functions for Memories in Stone.
"""

import os
import boto3
from boto3.dynamodb.conditions import Key
from typing import Optional, List
from shared.models import Order, OrderFile, OrderStatus, FileStatus, now_iso

_dynamodb = None


def get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb").Table(os.environ["ORDERS_TABLE"])
    return _dynamodb


# ── ORDER OPERATIONS ──────────────────────────────────────────────────────────

def create_order(order: Order) -> Order:
    """Persist a new order to DynamoDB."""
    get_table().put_item(Item=order.to_dynamo())
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
        expr_attrs[f":{key}"] = value

    get_table().update_item(
        Key={"PK": f"ORDER#{order_id}", "SK": "METADATA"},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=attr_names,
        ExpressionAttributeValues=expr_attrs,
    )


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
    get_table().put_item(Item=file.to_dynamo())
    return file


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
        expr_attrs[f":{key}"] = value

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


def all_files_complete(order_id: str) -> bool:
    """Return True if every file for an order has status DONE."""
    files = get_order_files(order_id)
    if not files:
        return False
    return all(f.status == FileStatus.DONE for f in files)


def any_file_failed(order_id: str) -> bool:
    """Return True if any file has permanently failed."""
    files = get_order_files(order_id)
    return any(f.status == FileStatus.FAILED for f in files)
