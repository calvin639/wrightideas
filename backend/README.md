# Memories in Stone — Backend

AWS SAM-based backend for the Memories in Stone ordering system.

## Architecture

```
Customer → API Gateway (HTTP API)
            ├── POST /orders                → CreateOrderFunction
            ├── GET  /orders/{id}           → GetOrderFunction
            ├── POST /orders/{id}/checkout  → CreateCheckoutFunction
            ├── POST /webhooks/stripe       → StripeWebhookFunction
            └── POST /webhooks/runway       → RunwayWebhookFunction

Payment confirmed → SQS (VideoGenerationQueue) → VideoGeneratorFunction
                    → Runway ML API (per file)
                    → Runway calls /webhooks/runway when each clip ready

All clips done → SQS (MontageQueue) → MontageBuilderFunction
                 → FFmpeg montage → S3
                 → QR code → S3
                 → Email customer via SES
```

## Prerequisites

- [AWS CLI](https://aws.amazon.com/cli/) configured with an IAM user/role for `eu-west-1`
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- Python 3.12
- Docker (for SAM local and Lambda layer builds)

Install SAM CLI on macOS:
```bash
brew install aws-sam-cli
```

On Linux:
```bash
pip install aws-sam-cli
```

---

## First-Time Setup

### 1. Create accounts & get API keys

| Service | URL | What you need |
|---|---|---|
| **Stripe** | https://dashboard.stripe.com | Secret key + Webhook secret |
| **Runway ML** | https://app.runwayml.com | API key |
| **AWS SES** | AWS Console → SES | Verify your sender email |

### 2. Verify your SES sender email

Before any emails will send, verify the address in SES:

```bash
aws ses verify-email-identity \
  --email-address orders@memories.wrightideas.co \
  --region eu-west-1
```

Check your inbox and click the verification link.

### 3. Store secrets in AWS SSM Parameter Store

```bash
make setup-secrets ENV=dev
make setup-secrets ENV=prod
```

This will prompt for each secret and store it securely in SSM.

### 4. Build and deploy to dev

```bash
# First deployment (interactive — sets up S3 bucket for SAM artifacts)
make deploy-guided

# Subsequent deployments
make deploy
```

### 5. Configure Stripe webhook

After first deploy, get your API Gateway URL from:
```bash
make outputs
```

In the Stripe dashboard → Webhooks → Add endpoint:
- URL: `https://YOUR_API_URL/dev/webhooks/stripe`
- Events: `checkout.session.completed`

Then update the webhook secret in SSM:
```bash
aws ssm put-parameter \
  --name "/memories/dev/stripe-webhook-secret" \
  --value "whsec_YOUR_SECRET" \
  --type SecureString --overwrite \
  --region eu-west-1
```

### 6. Set the API domain in SSM (for Runway webhooks)

```bash
aws ssm put-parameter \
  --name "/memories/dev/api-domain" \
  --value "YOUR_API_ID.execute-api.eu-west-1.amazonaws.com" \
  --type String --overwrite \
  --region eu-west-1
```

---

## FFmpeg Lambda Layer

The `MontageBuilderFunction` requires FFmpeg. You need to add a Lambda layer.

### Option A: Build it yourself (recommended)

```bash
# On an Amazon Linux 2 / Amazon Linux 2023 x86_64 machine or Docker:
docker run --rm -v $(pwd)/ffmpeg-layer:/output amazonlinux:2023 bash -c "
  yum install -y ffmpeg && \
  mkdir -p /output/bin && \
  cp /usr/bin/ffmpeg /output/bin/ && \
  cp /usr/bin/ffprobe /output/bin/
"

cd ffmpeg-layer
zip -r ../ffmpeg-layer.zip .

aws lambda publish-layer-version \
  --layer-name ffmpeg \
  --zip-file fileb://../ffmpeg-layer.zip \
  --compatible-runtimes python3.12 \
  --compatible-architectures x86_64 \
  --region eu-west-1
```

Copy the returned `LayerVersionArn` and add it to `template.yaml`:

```yaml
# In MontageBuilderFunction properties:
Layers:
  - !Ref DependenciesLayer
  - arn:aws:lambda:eu-west-1:YOUR_ACCOUNT_ID:layer:ffmpeg:1
```

### Option B: Use a public layer

See https://github.com/serverlesspub/ffmpeg-aws-lambda-layer for pre-built options.

---

## Background Music

For the title card + montage, the builder looks for:
```
src/functions/montage_builder/assets/gentle_music.mp3
```

Add any royalty-free or Artlist-licensed MP3 here. It should be:
- 3–10 minutes long (it loops/trims to match video length)
- Gentle, instrumental — appropriate for memorial content
- Not committed to git (add your own)

The Artlist Enterprise API integration point is marked with a `TODO` comment
in `montage_builder/handler.py` → `_get_background_music()`.

---

## Local Development

```bash
# Run API locally on port 3001
make local-api

# Invoke a specific function with a test event
make local-invoke fn=CreateOrderFunction event=events/create_order.json

# Tail live CloudWatch logs
make logs fn=CreateOrderFunction
```

Note: SQS-triggered functions (VideoGenerator, MontageBuilder) can't be
triggered locally via `start-api`. Use `local-invoke` with a mock SQS event.

---

## Deployment

```bash
# Dev
make deploy

# Production
make deploy-prod
```

Check deployment status:
```bash
make status       # Dev stack status
make outputs      # Dev stack outputs (API URL, bucket names, etc.)
```

---

## API Reference

### `POST /orders`
Create order + get presigned S3 upload URLs.

**Body:**
```json
{
  "customer_name": "Jane Murphy",
  "customer_email": "jane@example.com",
  "loved_one_name": "Michael Murphy",
  "loved_one_dob": "1945-03-12",
  "loved_one_dod": "2024-11-20",
  "stone_message": "Forever in our hearts",
  "stone_style": "black_slate",
  "stone_quantity": 1,
  "files": [
    {"filename": "photo.jpg", "content_type": "image/jpeg", "caption": "Christmas 2022"}
  ]
}
```

**Response:** `order_id`, presigned upload URLs per file.

Frontend must `PUT` each file directly to its `upload_url` (S3).

---

### `POST /orders/{order_id}/checkout`
Creates a Stripe Checkout session.

**Response:** `{ "checkout_url": "https://checkout.stripe.com/..." }`

Redirect customer to `checkout_url`.

---

### `GET /orders/{order_id}`
Returns order status + video/QR URLs when complete.

---

### `POST /webhooks/stripe`
Stripe webhook endpoint (configure in Stripe dashboard).

---

### `POST /webhooks/runway`
Runway ML callback (set as `callbackUrl` in Runway submissions).

---

## Pricing

| Stones | Price |
|--------|-------|
| 1      | €69.99 |
| 2      | €89.99 |
| 3      | €99.99 |
| 4+     | €99.99 + €14 per extra |

Defined in `src/shared/pricing.py`.

---

## Project Structure

```
backend/
├── template.yaml            SAM infrastructure definition
├── samconfig.toml           Deployment config (dev + prod)
├── Makefile                 Build, deploy, test commands
├── .env.example             Environment variable reference
├── env.local.json           Local dev values (not committed)
├── events/                  Test event payloads
├── layers/
│   └── dependencies/
│       └── requirements.txt Python packages (stripe, qrcode, etc.)
└── src/
    ├── shared/              Shared utilities (DB, pricing, email, QR)
    └── functions/
        ├── create_order/    POST /orders
        ├── get_order/       GET /orders/{id}
        ├── create_checkout/ POST /orders/{id}/checkout
        ├── stripe_webhook/  POST /webhooks/stripe
        ├── runway_webhook/  POST /webhooks/runway
        ├── video_generator/ SQS → Runway ML submission
        └── montage_builder/ SQS → FFmpeg montage + delivery
```
