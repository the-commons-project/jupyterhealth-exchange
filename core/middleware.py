from django.http import HttpResponse


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
        is_oauth = request.path.startswith("/o/")
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
