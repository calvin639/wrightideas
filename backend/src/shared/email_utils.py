"""
SES email notifications for order events.
"""
import os
import boto3
import logging

logger = logging.getLogger(__name__)
ses = boto3.client("ses", region_name="eu-west-1")

FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "orders@memories.wrightideas.co")


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
      <p>If you have any questions, reply to this email or contact us at <a href="mailto:calvin.wright639@gmail.com">calvin.wright639@gmail.com</a>.</p>
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
    admin_email = os.environ.get("ADMIN_EMAIL", "calvin.wright639@gmail.com")
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


def _send(to_email: str, subject: str, body_html: str) -> None:
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
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        # Don't raise — email failures shouldn't break the pipeline
