"""POST /api/v1/ow/webhook — OW notification receiver.

HMAC-SHA256 auth, daemon-thread dispatch. Dormant unless ow.ingest_mode=webhook.
Known: concurrent webhooks for same patient can produce duplicate observations
(TOCTOU race in dedup); stale OWPollEvent rows if thread dies mid-ingest.
"""

import hashlib
import hmac
import json
import logging
import threading

from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.services.ow_ingest import ingest_for_user
from core.services.ow_ingest._common import resolve_patient_from_ow_user_id
from core.services.ow_integration import load_and_validate_polling_config

logger = logging.getLogger(__name__)

MAX_WEBHOOK_BODY_BYTES = 64 * 1024  # 64 KB cap


def _verify_signature(body: bytes, header_value: str | None, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification of ``X-Signature: sha256=<hex>``.

    An empty ``secret`` (e.g. polling mode where the webhook is dormant)
    always returns False so an attacker who knows the empty-secret default
    cannot bypass auth by computing HMAC over an empty key.
    """
    if not secret:
        return False
    if not header_value or not header_value.startswith("sha256="):
        return False
    received = header_value[len("sha256=") :]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _ingest_in_background(patient_id: int, ow_user_id: str) -> None:
    """Run ingest_for_user in a daemon thread so the webhook returns 202 fast.

    Logs unhandled exceptions because no caller is waiting on the result.
    """
    try:
        ingest_for_user(patient_id, trigger="webhook")
    except Exception as e:  # noqa: BLE001 — last-line catch in a background thread
        logger.exception(
            "ow_webhook background ingest failed",
            extra={"ow_user_id": ow_user_id, "patient_id": patient_id, "err": str(e)},
        )


@csrf_exempt
@require_POST
def ow_webhook(request):
    # Body-size check FIRST (before reading request.body) to avoid DoS.
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > MAX_WEBHOOK_BODY_BYTES:
        return JsonResponse({"detail": "payload too large"}, status=413)

    try:
        cfg = load_and_validate_polling_config()
    except ImproperlyConfigured as e:
        logger.error("ow_webhook config error: %s", e)
        return JsonResponse({"detail": "configuration error"}, status=500)

    # Read body once. Django enforces DATA_UPLOAD_MAX_MEMORY_SIZE in addition
    # to the Content-Length check above.
    body = request.body
    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        return JsonResponse({"detail": "payload too large"}, status=413)

    # Verify HMAC BEFORE the mode check so attackers can't side-channel
    # whether the endpoint is currently active vs dormant. An empty
    # webhook_secret (the default in polling mode) makes _verify_signature
    # always return False, so the response is identical 401 in either mode.
    if not _verify_signature(body, request.headers.get("X-Signature"), cfg["webhook_secret"]):
        return JsonResponse({"detail": "bad or missing signature"}, status=401)

    if cfg["ingest_mode"] != "webhook":
        return JsonResponse({"detail": "webhook ingest is not active"}, status=503)

    try:
        payload = json.loads(body.decode("utf-8"))
        ow_user_id = payload["user_id"]
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError):
        return JsonResponse({"detail": "invalid payload"}, status=400)

    patient = resolve_patient_from_ow_user_id(str(ow_user_id))
    if patient is None:
        return JsonResponse({"detail": "unknown user"}, status=404)

    # Spawn the actual ingest in a background daemon thread so OW gets its
    # 202 within milliseconds. The thread inherits the Django app context;
    # since ingest_for_user opens its own DB transactions, no special setup
    # is required. Daemon=True so the worker process can exit cleanly.
    threading.Thread(
        target=_ingest_in_background,
        args=(patient.id, str(ow_user_id)),
        daemon=True,
        name=f"ow_webhook_ingest_{patient.id}",
    ).start()

    return JsonResponse({"status": "accepted"}, status=202)
