from django.conf import settings
from django.http import HttpResponse, JsonResponse

HEALTH_PATH = "/health"


class HealthCheckMiddleware:
    """``GET /health``: the liveness probe, answered before the rest of the chain runs.

    No database, no auth, and no Host check: an Application Load Balancer (and Fly's proxy)
    probes a container with the container's own IP address as the Host header, which
    ``CommonMiddleware`` would turn into a 400 under ``ALLOWED_HOSTS``. Sitting first in the
    chain, this answers the probe whatever Host it carries; every other path, and any other
    method on this one, goes on through the chain untouched.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == HEALTH_PATH and request.method in ("GET", "HEAD"):
            return JsonResponse({"status": "ok", "version": settings.JHE_VERSION})
        return self.get_response(request)


class OAuthCorsMiddleware:
    """Permissive CORS for the OAuth endpoints under /o/.

    Browser-based OAuth clients (SPA / PKCE) served from a different origin need
    to read the JSON token response from a cross-origin fetch, which the browser
    blocks unless the server sends CORS headers. This adds `Access-Control-*`
    headers (allowing any origin) and answers preflight OPTIONS requests, scoped
    to /o/ so the rest of the app is unaffected.

    Intended for demo/local clients (e.g. the tests/patient-access-client
    harness). Allowing any
    origin is acceptable here because the OAuth flow itself is still gated by
    registered redirect_uris and PKCE; tighten the allowed origin if this is ever
    used beyond local testing.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # The FHIR discovery documents (capabilities + SMART configuration) are
        # public, credential-free, read-only, and consumed cross-origin by
        # browser-based SMART apps — same reasoning as the /o/ endpoints.
        is_discovery = request.path.startswith("/FHIR/") and (
            request.path.endswith("/metadata") or request.path.endswith("/.well-known/smart-configuration")
        )
        is_oauth = request.path.startswith(settings.OAUTH_MOUNT_PATH) or is_discovery
        if is_oauth and request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)
        if is_oauth:
            response["Access-Control-Allow-Origin"] = "*"
            response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response["Access-Control-Max-Age"] = "86400"
        return response
