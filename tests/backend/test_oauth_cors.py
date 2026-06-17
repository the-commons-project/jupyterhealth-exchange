"""The OAuthCorsMiddleware adds permissive CORS to /o/ so browser-based OAuth
clients on another origin (e.g. the tests/patient-access-client demo) can read
the token response."""


def test_preflight_options_on_token_endpoint(client):
    resp = client.options("/o/token/")
    assert resp.status_code == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]


def test_cors_header_present_on_oauth_paths(client):
    # The authorize endpoint redirects unauthenticated users, but the CORS header
    # is still set on the way out.
    resp = client.get("/o/authorize/")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_no_cors_header_outside_oauth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "Access-Control-Allow-Origin" not in resp.headers
