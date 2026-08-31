"""
DEPRECATED — Runway is no longer the video generation provider.

Runway's API route to seedance blocks photographs of real people
(INPUT_PREPROCESSING.SAFETY.THIRD_PARTY) and its first-party models reject
the first/last keyframe array, so it could give us faces or identity pinning
but never both. Generation moved to fal.ai — see functions/video_generator.

This endpoint was already observe-only before the migration (Step Functions
polls for completion); it is now entirely inert. Retained so the route does
not 404, and deliberately NOT deleted yet.

POST /webhooks/runway  —  OBSERVE ONLY

Runway calls this when a task finishes. It deliberately writes nothing.

WHY IT IS INERT
Completion is owned by the Step Functions execution, which polls each task it
submitted. When this handler also wrote completion state there were two
independent writers deciding an order was finished, and they could both decide
it at once — producing two montages and two "your video is ready" emails to a
grieving customer. The webhook lost that job rather than the poller, because the
state machine only ever acts on tasks belonging to a live execution, whereas a
webhook can arrive for anything at any time.

The endpoint stays deployed because Runway is configured to call it and a 404
would show up as delivery failures on their side. It is also genuinely useful as
a timing signal in the logs: comparing the webhook timestamp against when the
poll loop noticed shows how much latency the polling interval is adding.
"""

import json
import logging

from shared.response import ok, error

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error("Invalid JSON")

    task_id = body.get("id")
    status = body.get("status")

    if not task_id or not status:
        return error("Missing id or status")

    # Logged, never acted on. If you are here because a clip went missing, the
    # answer is in the state machine execution history, not this function.
    logger.info(
        "Runway webhook (observed, no action): task=%s status=%s outputs=%s",
        task_id, status, len(body.get("output", []) or []),
    )
    return ok({"received": True})
