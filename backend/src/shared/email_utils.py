"""
SES email notifications for order events.
"""
import os
import boto3
import logging

logger = logging.getLogger(__name__)
ses = boto3.client("ses", region_name="eu-west-1")

FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "noreply@wrightideas.biz")

# Fallback recipient for admin notifications when ADMIN_EMAIL is unset.
#
# On an address in the SES-verified domain, deliberately. While SES is in
# sandbox, recipients must be verified identities, so an external mailbox
# (a personal gmail, say) is rejected at send time — and because every admin
# send is best-effort, that failure is a log line, not a visible error.
# ADMIN_EMAIL is set globally in template.yaml; this only covers local runs
# and any future function that forgets it.
ADMIN_EMAIL_FALLBACK = "calvin@wrightideas.biz"


def send_order_confirmation(order) -> None:
    """Send confirmation email when order is placed and paid."""
    subject = f"Your order is confirmed — Memories in Stone"
    body_html = f"""
    <html><body style="font-family: Georgia, serif; color: #2c2c2c; max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #4a3f35;">Thank you, {order.customer_name}</h2>
      <p>We've received your order and payment. We're now creating a beautiful memorial video for <strong>{order.loved_one_name}</strong>.</p>
      <div style="background: #f9f6f2; border-left: 4px solid #9c7c5e; padding: 16px; margin: 20px 0;">
        <p><strong>Order ID:</strong> {order.order_id[:8].upper()}</p>
        <p><strong>Stone:</strong> {order.stone_quantity}x Black Slate</p>
        <p><strong>In memory of:</strong> {order.loved_one_name}</p>
        <p><strong>Total paid:</strong> €{order.total_amount_cents/100:.2f}</p>
      </div>
      <p>Your memorial video will be ready within <strong>24 hours</strong>. We'll send you an email as soon as it's complete with a link to the tribute page.</p>
      <p>If you have any questions, reply to this email or contact us at <a href="mailto:calvin@wrightideas.biz">calvin@wrightideas.biz</a>.</p>
      <br>
      <p style="color: #9c7c5e; font-style: italic;">— The Memories in Stone team</p>
    </body></html>
    """
    _send(order.customer_email, subject, body_html)


def send_video_ready(order) -> None:
    """Send notification when the memorial video is ready."""
    subject = f"Your tribute video for {order.loved_one_name} is ready 🎬"
    body_html = f"""
    <html><body style="font-family: Georgia, serif; color: #2c2c2c; max-width: 600px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #4a3f35;">Your memorial video is ready</h2>
      <p>Dear {order.customer_name},</p>
      <p>We've finished creating the tribute video for <strong>{order.loved_one_name}</strong>. Scan the QR code on your stone — or click the link below — to watch it now.</p>
      <div style="text-align: center; margin: 30px 0;">
        <a href="{order.tribute_page_url}" 
           style="background: #4a3f35; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-size: 16px;">
          View Tribute Page →
        </a>
      </div>
      <p>Your stone is being prepared and will be shipped to you shortly. We'll send tracking information separately.</p>
      <p>Thank you for trusting us to honour someone so special.</p>
      <br>
      <p style="color: #9c7c5e; font-style: italic;">— The Memories in Stone team</p>
    </body></html>
    """
    _send(order.customer_email, subject, body_html)


def send_admin_new_order(order) -> None:
    """Notify admin of a new paid order."""
    admin_email = os.environ.get("ADMIN_EMAIL", ADMIN_EMAIL_FALLBACK)
    subject = f"[NEW ORDER] {order.loved_one_name} — €{order.total_amount_cents/100:.2f}"
    body_html = f"""
    <html><body style="font-family: monospace; padding: 20px;">
      <h3>New Order Received</h3>
      <pre>
Order ID:     {order.order_id}
Customer:     {order.customer_name} &lt;{order.customer_email}&gt;
Loved one:    {order.loved_one_name}
Stones:       {order.stone_quantity}x {order.stone_style}
Amount:       €{order.total_amount_cents/100:.2f}
Stripe:       {order.stripe_payment_intent}
Message:      {order.stone_message}
Status:       {order.status}
Created:      {order.created_at}
      </pre>
    </body></html>
    """
    _send(admin_email, subject, body_html)


def send_admin_review_request(order, items, decide_base: str) -> bool:
    """Ask the admin to sign off on prepared frames before Runway spend.

    Returns True only if SES accepted the message — the gate-mode caller
    auto-approves the order when this fails, so a broken email can never
    park an order for the full review window.

    `items`: [{file_id, filename, before_url, after_url, warnings,
    restore_meta}] from the review_notify function. `decide_base` is the
    decision endpoint with order and review key baked in; empty string means
    notify-only mode (no buttons, no gate).
    """
    admin_email = os.environ.get("ADMIN_EMAIL", ADMIN_EMAIL_FALLBACK)
    gate = bool(decide_base)
    subject = (
        f"[{'REVIEW NEEDED' if gate else 'PREP REPORT'}] "
        f"{order.loved_one_name} — {len(items)} photo(s) prepared"
    )

    rows = []
    for it in items:
        warn_html = ""
        if it["warnings"]:
            warn_lines = "".join(f"<li>{w}</li>" for w in it["warnings"])
            warn_html = (
                f'<ul style="color:#a33; margin:6px 0 0 0; padding-left:18px;'
                f' font-size:13px;">{warn_lines}</ul>'
            )
        rm = it.get("restore_meta") or {}
        detail = (
            f"weight {rm.get('weight', '—')} · faces {rm.get('face_count', '—')} "
            f"· median {rm.get('median_face_px', '—')}px · tier {rm.get('tier', '—')}"
        )
        before_img = (
            f'<td style="width:50%; padding:4px;"><img src="{it["before_url"]}" '
            f'style="width:100%; border:1px solid #ccc;"><br>'
            f'<small>before (unenhanced)</small></td>'
            if it["before_url"] else
            '<td style="width:50%; padding:4px; color:#999;"><small>no control frame</small></td>'
        )
        buttons = ""
        if gate:
            buttons = (
                f'<a href="{decide_base}&action=use_before&file={it["file_id"]}" '
                f'style="font-size:13px; margin-right:12px;">use unenhanced instead</a>'
                f'<a href="{decide_base}&action=use_enhanced&file={it["file_id"]}" '
                f'style="font-size:13px;">use enhanced</a>'
            )
        rows.append(f"""
        <div style="margin:24px 0; padding-bottom:16px; border-bottom:1px solid #ddd;">
          <p style="margin:0 0 4px 0;"><strong>{it['filename'] or it['file_id']}</strong><br>
             <small style="color:#666;">{detail}</small></p>
          {warn_html}
          <table style="width:100%; border-collapse:collapse;"><tr>
            {before_img}
            <td style="width:50%; padding:4px;"><img src="{it['after_url']}"
                style="width:100%; border:1px solid #ccc;"><br>
                <small>after (submitting this)</small></td>
          </tr></table>
          {buttons}
        </div>""")

    approve_html = ""
    if gate:
        approve_html = f"""
      <div style="text-align:center; margin:30px 0;">
        <a href="{decide_base}&action=approve_all"
           style="background:#2d5a2d; color:white; padding:14px 28px;
                  text-decoration:none; border-radius:6px; font-size:16px;">
          Approve — send to video generation →
        </a>
      </div>
      <p style="color:#888; font-size:13px;">The order waits for this approval.
      If no decision is made before the review window closes, it proceeds
      automatically with the frames shown above.</p>"""

    body_html = f"""
    <html><body style="font-family: Georgia, serif; color: #2c2c2c; max-width: 640px; margin: 0 auto; padding: 20px;">
      <h2 style="color: #4a3f35;">Photo review — {order.loved_one_name}</h2>
      <p>Order <strong>{order.order_id[:8].upper()}</strong> ·
         {order.customer_name} · {len(items)} photo(s) prepared</p>
      {''.join(rows)}
      {approve_html}
    </body></html>
    """
    return _send(admin_email, subject, body_html)


def send_admin_failure_alert(order, stage: str, cause: str, files: list) -> bool:
    """Alert the admin that an order failed. ADMIN ONLY — never the customer.

    A failure is frequently fixable without the customer ever knowing (credits,
    an expired URL, a transient provider error), and a "something went wrong"
    email to someone who has just paid for a memorial video causes alarm that
    the fix would have made unnecessary. So this goes to the admin with enough
    detail to act on, and the customer hears nothing until there is something
    real to tell them.

    `files` is the list of OrderFile records, so per-file errors are included.
    """
    admin_email = os.environ.get("ADMIN_EMAIL", ADMIN_EMAIL_FALLBACK)
    subject = f"[FAILED] {order.loved_one_name} — order {order.order_id[:8].upper()} stopped at {stage}"

    rows = []
    for f in files:
        state = f.status or "?"
        colour = "#a33" if state == "FAILED" else "#2c2c2c"
        detail = (f.error_message or "")[:300] or "—"
        rows.append(
            f'<tr>'
            f'<td style="padding:4px 8px; border-bottom:1px solid #eee;">{f.original_filename or f.file_id}</td>'
            f'<td style="padding:4px 8px; border-bottom:1px solid #eee; color:{colour};"><strong>{state}</strong></td>'
            f'<td style="padding:4px 8px; border-bottom:1px solid #eee; font-size:12px;">{detail}</td>'
            f'</tr>'
        )

    body_html = f"""
    <html><body style="font-family: -apple-system, Helvetica, sans-serif; color:#2c2c2c; max-width:760px; margin:0 auto; padding:20px;">
      <h2 style="color:#a33;">Order failed — {order.loved_one_name}</h2>
      <p style="font-size:15px;">Stopped at: <strong>{stage}</strong></p>
      <div style="background:#fff5f5; border-left:4px solid #a33; padding:12px; margin:16px 0;">
        <pre style="margin:0; white-space:pre-wrap; font-size:13px;">{cause}</pre>
      </div>
      <table style="border-collapse:collapse; width:100%; font-size:14px;">
        <tr style="text-align:left; background:#f5f5f5;">
          <th style="padding:6px 8px;">File</th><th style="padding:6px 8px;">Status</th><th style="padding:6px 8px;">Error</th>
        </tr>
        {''.join(rows)}
      </table>
      <h3 style="margin-top:24px; font-size:15px;">Order</h3>
      <pre style="font-size:13px; background:#f9f9f9; padding:12px;">
Order ID:  {order.order_id}
Customer:  {order.customer_name} &lt;{order.customer_email}&gt;
Status:    {order.status}
Review:    {getattr(order, 'review_status', '') or '—'}
Created:   {order.created_at}
      </pre>
      <p style="color:#666; font-size:13px;">The customer has <strong>not</strong> been notified.
      Check the Step Functions execution <code>order-{order.order_id}</code> in eu-west-1,
      then see backend/RUNBOOK.md &sect;5.</p>
    </body></html>
    """
    return _send(admin_email, subject, body_html)


def _send(to_email: str, subject: str, body_html: str) -> bool:
    """Send one email. Returns True on success, False on failure.

    Never raises — email failures must not break the pipeline — but callers
    that DEPEND on delivery (the review gate: an unsent review email means a
    12-hour stall for nothing) must check the return value and degrade.
    """
    try:
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": body_html, "Charset": "UTF-8"}},
            },
        )
        logger.info(f"Email sent to {to_email}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False
