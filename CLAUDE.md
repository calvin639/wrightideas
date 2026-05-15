# Wright Ideas — Project Guide

This file is the first thing to read when starting work in this repo. For deployment steps, see `DEPLOYMENT.md`.

## What this repo is

`wrightideas` hosts two things:

1. **The wrightideas.biz marketing site** — a static HTML site (plus a small Next.js static export under `_next/`) that lives at the root of the repo.
2. **Memories in Stone** — a paid product that ships a memorial video built from customer-uploaded photos and videos. The product page lives under `/memories/` on the site, and its backend (orders, payments, AI video generation, montage assembly, delivery) lives in `backend/` as an AWS SAM application.

The separate **eRate** project (served at `erate.wrightideas.biz`) is **not** in this repo. It lives in a sibling folder at the same level as `wrightideas/` and is deployed independently.

## Top-level layout

```
wrightideas/
├── index.html, about.html, products.html, ...   Static marketing pages
├── custom.css, globals.css                      Site styles
├── images/                                      Site imagery
├── _next/                                       Committed Next.js static export
├── memories/                                    Memories in Stone frontend
│   ├── index.html                               Product landing/order flow
│   ├── tribute.html                             Live tribute page (reads from backend)
│   └── tribute.css, memories.css
├── backend/                                     AWS SAM app — Memories in Stone API
│   ├── template.yaml                            SAM infra (API GW, Lambda, SQS, DynamoDB, S3, CloudFront)
│   ├── samconfig.toml                           dev + prod deploy profiles
│   ├── Makefile                                 build / deploy / logs / secrets
│   ├── scripts/deploy.sh                        Deploy wrapper (sources env vars)
│   ├── src/
│   │   ├── shared/                              DB, pricing, email, QR helpers
│   │   └── functions/                           One dir per Lambda
│   │       ├── create_order/                    POST /orders
│   │       ├── get_order/                       GET  /orders/{id}
│   │       ├── create_checkout/                 POST /orders/{id}/checkout
│   │       ├── stripe_webhook/                  POST /webhooks/stripe
│   │       ├── runway_webhook/                  POST /webhooks/runway
│   │       ├── video_generator/                 SQS -> Runway ML submission
│   │       └── montage_builder/                 SQS -> FFmpeg montage + delivery
│   ├── layers/dependencies/                     Python deps layer (stripe, qrcode, etc.)
│   └── events/                                  Sample SAM local invoke events
├── archive/                                     Old site versions (don't touch unless asked)
├── .github/workflows/deploy.yml                 GitHub Actions — auto-deploy site on push to main
└── .gitignore
```

## Frontend stack

Plain static HTML + CSS. No build step is required for the marketing pages — edit the `.html` files directly. The `_next/` directory is a committed static export and should generally be left alone unless regenerating it on purpose.

The production site is served from **S3 + CloudFront** at `https://wrightideas.biz` (and `memories.wrightideas.biz`). Domain is managed via Route 53 / CloudFront.

## Backend stack (Memories in Stone)

AWS SAM, Python 3.12, x86_64, region **eu-west-1**. CloudFormation stack names:

- `memories-in-stone-dev`
- `memories-in-stone-prod`

Order flow:

```
Customer order -> API GW -> CreateOrderFunction (DynamoDB + presigned S3 uploads)
Customer pays  -> Stripe Checkout -> StripeWebhook -> SQS(VideoGenerationQueue)
               -> VideoGeneratorFunction -> Runway ML API (one clip per file)
Runway done    -> RunwayWebhook -> SQS(MontageQueue) when all clips ready
               -> MontageBuilderFunction -> FFmpeg montage + QR code -> S3
               -> SES email to customer with tribute link
```

Secrets are stored in **AWS SSM Parameter Store** under `/memories/{env}/...`. The `backend/scripts/deploy.sh` script sources API keys from `~/.bashrc` (`STRIPE_SANDBOX_KEY`, `STRIPE_SECRET_KEY`, `RUNWAY_AI_KEY`, `STRIPE_WEBHOOK_SECRET`, `RUNWAY_WEBHOOK_URL`) — it will not deploy without them.

The `MontageBuilderFunction` needs an **FFmpeg Lambda layer** — see the "FFmpeg Lambda Layer" section of `backend/README.md` for build/publish instructions.

## Pricing

Defined in `backend/src/shared/pricing.py`:

| Stones | Price |
|--------|-------|
| 1      | €69.99 |
| 2      | €89.99 |
| 3      | €99.99 |
| 4+     | €99.99 + €14 per extra |

## Conventions

Editing a marketing page means editing the `.html` directly and committing — the GitHub Actions workflow syncs `main` to S3 and invalidates CloudFront. Do **not** commit `.env`, `env.local.json`, `src/functions/montage_builder/assets/gentle_music.mp3`, or any API keys. Secrets go in SSM, never in the repo. When adding a new Lambda, add it to `template.yaml`, put the handler under `backend/src/functions/<name>/`, and add a sample event in `backend/events/` if it's API- or SQS-triggered.

## Where to go next

- Deploying anything: `DEPLOYMENT.md`
- Backend detail (API payloads, FFmpeg layer, Artlist music integration TODO): `backend/README.md`
- Marketing copy / page text: the `*.txt` files at the repo root are plaintext drafts that correspond to the `.html` pages.
