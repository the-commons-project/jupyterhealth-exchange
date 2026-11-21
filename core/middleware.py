import ipaddress

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse

from core.services.jhe_settings import get_setting

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


# Reachable only from `auth.admin.allowed_cidrs`: the Django admin, and the settings API (it edits the
# allowlist itself and the SSO switches). The rest of the site, the JHE admin portal included, is
# gated by SSO login, not by network.
RESTRICTED_PREFIXES = ("/admin/", "/api/v1/jhe_settings")

# Closed while SSO-only is active: account creation and password recovery by email have no use
# when practitioners sign in through SSO and patients by invitation.
SSO_ONLY_CLOSED_PREFIXES = (
    "/allauth/signup/",
    "/allauth/password/reset/",
    "/accounts/password_reset/",
    "/accounts/reset/",
)


def client_ip(request, proxy_hops=None):
    """The address the request really came from, behind `proxy_hops` trusted proxies.

    Each proxy appends the address it saw to X-Forwarded-For, so the entries to the left of the
    trusted ones are whatever the client claimed and can be forged; the entry `proxy_hops` from
    the right is the one the nearest untrusted hop was seen as. An Application Load Balancer is
    one hop (TRUSTED_PROXY_HOPS=1). With no header, the peer address is the client.
    """
    hops = settings.TRUSTED_PROXY_HOPS if proxy_hops is None else proxy_hops
    forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    candidate = forwarded[-hops] if hops and len(forwarded) >= hops else request.META.get("REMOTE_ADDR", "")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def parse_networks(value):
    """Comma- or whitespace-separated CIDR blocks (or single addresses) as networks; raises ValueError."""
    entries = [e for e in value.replace(",", " ").split() if e]
    return [ipaddress.ip_network(e, strict=False) for e in entries]


class AdminIpAllowlistMiddleware:
    """Restrict /admin/ (the Django admin, login included) and the settings API to the networks in
    `auth.admin.allowed_cidrs`.

    Empty means no restriction. When set, a client outside every listed block gets 403 before the
    view runs, whether or not it holds a session, so a stolen session or password is no use
    from elsewhere. The rest of the site is untouched. Fails closed: an address that cannot be
    read from the request is refused, and a value with no usable block refuses everyone (the
    setting's validator keeps that from being saved). To get back in after a bad value, set the
    setting from the JHE admin portal or `manage.py shell` instead of /admin/.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(RESTRICTED_PREFIXES):
            allowed = (get_setting("auth.admin.allowed_cidrs", "") or "").strip()
            if allowed:
                ip = client_ip(request)
                networks = []
                for entry in allowed.replace(",", " ").split():
                    try:
                        networks.append(ipaddress.ip_network(entry, strict=False))
                    except ValueError:
                        continue
                if ip is None or not any(ip.version == n.version and ip in n for n in networks):
                    return HttpResponseForbidden("The admin site is not available from this network.")
        return self.get_response(request)


class SsoOnlyClosedPathsMiddleware:
    """While SSO-only is active (core.context_processors.sso_only_active), answer 403 for the
    allauth sign-up and the password-reset routes. Anyone could otherwise create an account at
    /allauth/signup/ with no invite code, and reset flows confirm which emails exist."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(SSO_ONLY_CLOSED_PREFIXES):
            from core.context_processors import sso_only_active

            if sso_only_active():
                return HttpResponseForbidden("Not available: sign in with SAML2.")
        return self.get_response(request)
