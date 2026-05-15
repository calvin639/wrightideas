# Deployment Guide

Single source of truth for how to deploy everything in this repo. There are two independent deploy paths:

1. **Frontend** — the static site at `wrightideas.biz` / `memories.wrightideas.biz`. Auto-deploys from `main` via GitHub Actions.
2. **Backend** — the Memories in Stone SAM application in `backend/`. Deployed manually with `make deploy` / `make deploy-prod`.

The sibling `erate` project (served at `erate.wrightideas.biz`) is deployed separately from its own folder — not covered here.

---

## 1. Frontend (static site)

### Automatic — on push to `main`

A push to `main` triggers `.github/workflows/deploy.yml`, which:

1. Checks out the repo.
2. Configures AWS credentials from the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` GitHub secrets (region `us-east-1`).
3. Runs `aws s3 sync . s3://$S3_BUCKET_NAME --exclude ".git/*" --exclude ".github/*" --delete`.
4. Invalidates the CloudFront distribution listed in the `CLOUDFRONT_DISTRIBUTION_ID` secret (path `/*`).

**Required GitHub secrets** (set under repo -> Settings -> Secrets and variables -> Actions):

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `S3_BUCKET_NAME`
- `CLOUDFRONT_DISTRIBUTION_ID`

### Typical change flow

```bash
# Edit index.html / products.html / custom.css / ...
git add .
git commit -m "copy: tweak hero headline"
git push origin main
# Watch the run at: https://github.com/calvin639/wrightideas/actions
```

Changes should be live within ~1 minute of the CloudFront invalidation completing.

### Manual frontend deploy (fallback)

If GitHub Actions is broken and you need to push urgently:

```bash
aws s3 sync . s3://YOUR_BUCKET_NAME \
  --exclude ".git/*" \
  --exclude ".github/*" \
  --exclude "backend/*" \
  --exclude "memories/tribute/*" \
  --delete

aws cloudfront create-invalidation \
  --distribution-id YOUR_DISTRIBUTION_ID \
  --paths "/*"
```

Grab the bucket name and distribution id from the GitHub Actions secrets, from the AWS console, or from `aws cloudfront list-distributions`.

---

## 2. Backend (Memories in Stone / AWS SAM)

Region: **`eu-west-1`**. Stacks: `memories-in-stone-dev`, `memories-in-stone-prod`.

### Prerequisites (one-time per machine)

- AWS CLI configured with credentials that can deploy to `eu-west-1`.
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) (`brew install aws-sam-cli` on macOS).
- Docker running (SAM build uses it for the Python dependencies layer).
- Python 3.12.

### Required environment variables

`backend/scripts/deploy.sh` reads these from `~/.bashrc`. Set them once and source your shell:

```bash
# Stripe — either name works; SECRET takes precedence over SANDBOX
export STRIPE_SANDBOX_KEY="sk_test_..."         # dev
export STRIPE_SECRET_KEY="sk_live_..."          # prod
export STRIPE_WEBHOOK_SECRET="whsec_..."        # set after first deploy

# Runway ML
export RUNWAY_AI_KEY="..."
export RUNWAY_WEBHOOK_URL="https://YOUR_API/dev/webhooks/runway"  # set after first deploy
```

Deploy will abort if `STRIPE_*` or `RUNWAY_AI_KEY` are missing. The webhook secret and Runway URL are allowed to be placeholders on the very first deploy — you fill them in afterwards.

### First-time setup (per environment)

From the `backend/` directory:

```bash
# 1. Verify the SES sender email (once per AWS account)
aws ses verify-email-identity \
  --email-address orders@memories.wrightideas.co \
  --region eu-west-1
# Click the verification link in the inbox.

# 2. Store secrets in SSM Parameter Store
make setup-secrets ENV=dev
make setup-secrets ENV=prod

# 3. First deploy (interactive)
make deploy-guided

# 4. After deploy, read the API Gateway URL
make outputs

# 5. Point Stripe at the webhook endpoint:
#    Stripe dashboard -> Webhooks -> Add endpoint
#    URL:    https://YOUR_API_URL/dev/webhooks/stripe
#    Events: checkout.session.completed
#    Copy the signing secret and update SSM:
aws ssm put-parameter \
  --name "/memories/dev/stripe-webhook-secret" \
  --value "whsec_YOUR_SECRET" \
  --type SecureString --overwrite --region eu-west-1

# 6. Record the API domain in SSM (used when submitting jobs to Runway)
aws ssm put-parameter \
  --name "/memories/dev/api-domain" \
  --value "YOUR_API_ID.execute-api.eu-west-1.amazonaws.com" \
  --type String --overwrite --region eu-west-1
```

### Publish the FFmpeg Lambda layer (one-time)

`MontageBuilderFunction` needs FFmpeg. Build the layer with Docker on Amazon Linux 2023:

```bash
docker run --rm -v $(pwd)/ffmpeg-layer:/output amazonlinux:2023 bash -c "
  yum install -y ffmpeg && mkdir -p /output/bin && \
  cp /usr/bin/ffmpeg /output/bin/ && cp /usr/bin/ffprobe /output/bin/
"
cd ffmpeg-layer && zip -r ../ffmpeg-layer.zip . && cd ..

aws lambda publish-layer-version \
  --layer-name ffmpeg \
  --zip-file fileb://ffmpeg-layer.zip \
  --compatible-runtimes python3.12 \
  --compatible-architectures x86_64 \
  --region eu-west-1
```

Copy the returned `LayerVersionArn` into `backend/template.yaml` under `MontageBuilderFunction.Properties.Layers`, then redeploy.

### Routine deploy

```bash
cd backend

# Validate before deploying (optional but fast)
make validate

# Dev (no prompt)
make deploy

# Production (3-second safety pause, changeset confirmation prompt)
make deploy-prod
```

### Common operations

```bash
# Inspect stack status / outputs
make status
make outputs

# Tail logs for a function
make logs fn=CreateOrderFunction
make logs-prod fn=StripeWebhookFunction

# Run the API locally
make local-api                    # http://localhost:3001

# Invoke one function locally with a fixture event
make local-invoke fn=CreateOrderFunction event=events/create_order.json

# Clean build artifacts
make clean
```

---

## 3. Pre-deploy checklist

Frontend:

- The change renders correctly when opening the `.html` file in a browser locally.
- Relative links still resolve (`/about`, `/memories/`, etc.).
- `_next/` hasn't been touched unless you're intentionally regenerating the Next.js export.

Backend:

- `make validate` passes (SAM lint + CFN checks).
- Any new Lambda has an entry in `template.yaml` and a sample event in `events/` if it's API- or SQS-triggered.
- New secrets are added to SSM via `make setup-secrets` (or a `put-parameter` call) — never hard-coded.
- If deploying to prod, you've tested the same change on dev first.

---

## 4. Rollback

**Frontend:** revert the commit on `main` and push; GitHub Actions will re-sync the previous state.

**Backend:** CloudFormation keeps prior template versions. Roll back via the console (CloudFormation -> stack -> Stack actions -> Rollback) or by redeploying the previous git SHA:

```bash
git checkout <previous-sha>
cd backend && make deploy          # or deploy-prod
git checkout main
```

Data in DynamoDB / S3 is not affected by a code rollback, but changes to table schemas in `template.yaml` are — review the changeset before confirming a prod rollback.
