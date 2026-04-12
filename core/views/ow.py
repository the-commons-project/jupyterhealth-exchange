"""Open Wearables proxy endpoints.

Thin REST wrappers around the OWIntegrationService for the patient frontend.
Currently exposes the provider list and OAuth callback proxy.
"""
import logging
import re
from urllib.parse import urlencode, urljoin

from django.http import HttpResponseRedirect
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.services.ow_integration import _get_ow_config, ow_service

logger = logging.getLogger(__name__)

# Allow-list for the provider URL parameter. Provider keys are short
# lowercase identifiers (e.g., "oura", "garmin", "whoop"). Anything else
# is rejected before we touch the OW URL.
_PROVIDER_KEY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

_GENERIC_OW_ERROR = {"error": "Open Wearables is currently unavailable."}
_GENERIC_NOT_CONFIGURED_ERROR = {
    "error": (
        "Open Wearables is not configured on this server. "
        "Contact your administrator."
    )
}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_providers(request):
    """GET /api/v1/ow/providers — Return enabled cloud OAuth providers from OW.

    Rewrites icon_url paths to absolute OW URLs so the frontend can render
    them directly. Returns a generic error message on failure (real details
    are logged server-side, never returned to the client, to avoid leaking
    config key names or stack traces).
    """
    try:
        ow_base_url, _ = _get_ow_config()
    except ValueError as e:
        logger.error(f"OW not configured: {e}")
        return Response(_GENERIC_NOT_CONFIGURED_ERROR, status=503)

    try:
        providers = ow_service.list_providers()
    except Exception as e:
        logger.error(f"OW providers request failed: {e}")
        return Response(_GENERIC_OW_ERROR, status=502)

    if not isinstance(providers, list):
        logger.error(f"OW providers returned unexpected shape: {type(providers).__name__}")
        return Response(_GENERIC_OW_ERROR, status=502)

    # Rewrite relative icon URLs to point to OW backend
    for p in providers:
        if not isinstance(p, dict):
            continue
        icon = p.get("icon_url") or p.get("iconUrl")
        if icon and isinstance(icon, str) and icon.startswith("/"):
            p["icon_url"] = urljoin(ow_base_url + "/", icon.lstrip("/"))
            p["iconUrl"] = p["icon_url"]

    return Response(providers)


@api_view(["GET"])
@permission_classes([AllowAny])
def provider_callback_proxy(request, provider):
    """GET /api/v1/oauth/<provider>/callback — Proxy OAuth callback to OW.

    Wearable providers (Oura, Garmin, etc.) redirect here after the user
    authorizes. We forward the browser to OW's callback endpoint which
    handles the token exchange. This lets us register a single callback
    URL with each provider (the JHE host) instead of having to expose OW.

    AllowAny is required because the user has just returned from a third-
    party OAuth flow and is not yet authenticated to JHE in this request.
    The provider name is validated against an allow-list to prevent
    open-redirect / SSRF attacks.
    """
    # Validate provider against allow-list — short lowercase keys only.
    # Without this an attacker could craft a URL like
    # /api/v1/oauth/..%2F..%2Fevil/callback that, depending on URL parsing,
    # could redirect to an attacker-controlled host.
    if not _PROVIDER_KEY_RE.fullmatch(provider):
        logger.warning(f"Rejected invalid provider in callback proxy: {provider!r}")
        return Response({"error": "Invalid provider."}, status=400)

    try:
        ow_base_url, _ = _get_ow_config()
    except ValueError as e:
        logger.error(f"OW not configured: {e}")
        return Response(_GENERIC_NOT_CONFIGURED_ERROR, status=503)

    # Build the redirect URL via urljoin so we can never escape the OW host,
    # even if a future change to the regex lets unexpected characters through.
    redirect_url = urljoin(
        ow_base_url + "/",
        f"api/v1/oauth/{provider}/callback",
    )
    # Pass through query params via urlencode to avoid string concatenation
    # bugs (e.g., a `state` value containing `&`).
    if request.GET:
        redirect_url = f"{redirect_url}?{urlencode(request.GET)}"

    response = HttpResponseRedirect(redirect_url)
    # OAuth callback URLs contain an authorization code that must not be
    # cached by intermediaries.
    response["Cache-Control"] = "no-store"
    return response
