"""
API endpoint: redeem review decisions from the admin review email.

GET NEVER MUTATES. A GET on a decision link renders a confirmation page whose
form POSTs back; only that POST performs the change. The route is Method: ANY
so both reach this handler. This is not ceremony: mail scanners, link
prefetchers and chat unfurlers (Outlook Safe Links, corporate gateways) follow
GET links in email, so a bare state-changing GET means a virus scanner can
approve an order. They do not submit forms.

    GET  ?order={id}&key={k}&action=approve_all              -> confirm page
    POST ?order={id}&key={k}&action=approve_all              -> approves
    GET  ?order={id}&key={k}&action=use_before&file={id}     -> confirm page
    POST ?order={id}&key={k}&action=use_before&file={id}     -> swaps frame
    POST ?order={id}&key={k}&action=use_enhanced&file={id}   -> swaps frame

The one GET that does real work is non-mutating by design:

    GET  ?order={id}&key={k}&action=image&file={id}&which=before|after

which 302-redirects to a fresh short-lived presigned URL. The review email's
<img> tags point here rather than carrying long-lived presigns, because a
presigned URL dies with the Lambda session credentials that signed it — a
"24 hour" URL can 403 hours before a 12-hour review window closes.

The review_key is a per-order 128-bit secret generated when the review opened
and stored on the order — the same trust model as a password reset link. Every
request re-validates it, including the image redirects.

`use_before` / `use_enhanced` flip which frame the file submits to Runway (the
prepared keys are deterministic, so flipping is just a pointer swap — nothing
is copied or lost). They can be clicked any number of times before approval.
`approve_all` redeems the Step Functions task token, which releases the
pipeline into GenerateClips. If the review window already timed out, the token
redeem fails gracefully — the pipeline has already proceeded, and the page
says so rather than pretending.
"""

import html
import logging
import os

import boto3

from shared.db import get_order, get_order_file, update_file_status, update_order_status
from shared.models import FileStatus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")

s3 = boto3.client("s3")
sfn = boto3.client("stepfunctions")

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Memories in Stone — Review</title></head>
<body style="font-family: Georgia, serif; color: #2c2c2c; max-width: 640px;
             margin: 40px auto; padding: 0 20px;">
  <h2 style="color: #4a3f35;">{title}</h2>
  <p>{body}</p>
</body></html>"""


def _page(status: int, title: str, body: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": PAGE.format(title=html.escape(title), body=body),
    }


CONFIRM_COPY = {
    "approve_all": ("Approve this order?",
                    "Every photo will be sent to video generation as shown "
                    "in the review email."),
    "use_before": ("Use the unenhanced version?",
                   "This photo will use its original, unenhanced frame."),
    "use_enhanced": ("Use the enhanced version?",
                     "This photo will use its enhanced frame."),
}


def _confirm_page(action: str, query: dict) -> dict:
    """GET renders a button; only the POST acts.

    Email link prefetchers (Outlook Safe Links, corporate scanners, chat
    unfurlers) follow GETs — a bare state-changing GET link means a virus
    scanner can approve an order. They do not submit forms.
    """
    title, blurb = CONFIRM_COPY[action]
    qs = "&".join(f"{k}={html.escape(str(v))}" for k, v in query.items())
    body = f"""{html.escape(blurb)}
      <form method="POST" action="?{qs}" style="margin-top:24px;">
        <button type="submit"
                style="background:#2d5a2d; color:white; border:none;
                       padding:14px 28px; border-radius:6px; font-size:16px;
                       font-family:inherit; cursor:pointer;">
          Confirm
        </button>
      </form>"""
    return _page(200, title, body)


def lambda_handler(event, context):
    q = event.get("queryStringParameters") or {}
    order_id = q.get("order")
    key = q.get("key")
    action = q.get("action")
    file_id = q.get("file")
    method = (
        event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    )

    if not order_id or not key or not action:
        return _page(400, "Missing parameters", "This link is incomplete.")

    order = get_order(order_id)
    if not order or not order.review_key or order.review_key != key:
        # Same response for "no such order" and "wrong key" — no oracle.
        return _page(403, "Link not valid", "This review link is not valid.")

    # Non-mutating: serve a review image via fresh short-lived presign.
    if action == "image" and file_id:
        return _image_redirect(order, file_id, q.get("which", "after"))

    if action not in CONFIRM_COPY:
        return _page(400, "Unknown action", "This link is not recognised.")
    if action in ("use_before", "use_enhanced") and not file_id:
        return _page(400, "Missing parameters", "This link is incomplete.")

    if method != "POST":
        return _confirm_page(action, q)

    if action == "approve_all":
        return _approve_all(order)
    return _swap_file(order, file_id, action)


def _image_redirect(order, file_id: str, which: str) -> dict:
    """302 to a fresh short-lived presign for one of this order's frames.

    `file_id` is checked against the order before it is interpolated into an
    S3 key, the same ownership check _swap_file does. Nothing exploitable is
    known to slip through without it — S3 keys are literal, so `..` buys no
    traversal — but this is the one endpoint that puts a caller-supplied id
    straight into a key, and the check is already written.
    """
    if not get_order_file(order.order_id, file_id):
        return _page(404, "File not found", "This file is not on the order.")
    suffix = "_before.jpg" if which == "before" else ".jpg"
    key = f"prepared/{order.order_id}/{file_id}{suffix}"
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": UPLOADS_BUCKET, "Key": key},
        ExpiresIn=600,
    )
    return {"statusCode": 302, "headers": {"Location": url}, "body": ""}


def _approve_all(order) -> dict:
    if order.review_status == "APPROVED":
        return _page(200, "Already approved",
                     "This order was already approved — nothing more to do.")
    if order.review_status != "PENDING" or not order.review_task_token:
        return _page(200, "Review closed",
                     "The review window for this order has closed and the "
                     "pipeline has already continued.")
    try:
        sfn.send_task_success(taskToken=order.review_task_token, output='{"approved": true}')
    except Exception as e:
        # Most likely the state timed out and auto-approved. Not an error the
        # admin can act on — tell them what actually happened.
        logger.warning("Token redeem failed for %s: %s", order.order_id, e)
        update_order_status(order.order_id, order.status,
                            review_status="AUTO_APPROVED", review_task_token="")
        return _page(200, "Review window expired",
                     "The review window had already expired, so the order "
                     "proceeded automatically. No action is needed.")

    update_order_status(order.order_id, order.status,
                        review_status="APPROVED", review_task_token="")
    logger.info("Order %s approved for generation", order.order_id)
    return _page(200, "Approved ✓",
                 "The order has been released for video generation. "
                 "You can close this page.")


def _swap_file(order, file_id: str, action: str) -> dict:
    if order.review_status != "PENDING":
        return _page(200, "Review closed",
                     "The review window for this order has closed — file "
                     "choices can no longer be changed.")
    f = get_order_file(order.order_id, file_id)
    if not f:
        return _page(404, "File not found", "This file is not on the order.")

    enhanced_key = f"prepared/{order.order_id}/{file_id}.jpg"
    before_key = f"prepared/{order.order_id}/{file_id}_before.jpg"

    if action == "use_before":
        try:
            s3.head_object(Bucket=UPLOADS_BUCKET, Key=before_key)
        except Exception:
            return _page(409, "No unenhanced version",
                         "No unenhanced control frame exists for this file "
                         "(KEEP_SOURCE_COPY may have been off).")
        target, label = before_key, "unenhanced"
    else:
        target, label = enhanced_key, "enhanced"

    meta = dict(f.restore_meta or {})
    meta["review_decision"] = label
    update_file_status(order.order_id, file_id, FileStatus.PREPARED,
                       prepared_s3_key=target, restore_meta=meta)
    logger.info("Order %s file %s -> %s frame", order.order_id, file_id, label)
    return _page(200, f"Using the {label} version",
                 f"This photo will use its <strong>{label}</strong> frame. "
                 "Return to the email to review other photos, and click "
                 "<em>Approve</em> when you're done.")
