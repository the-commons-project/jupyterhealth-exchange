import dataclasses
import urllib.parse

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jhe_mcp.auth import broker_state, pkce
from jhe_mcp.auth.broker import build_broker_router
from jhe_mcp.config import Settings


def _settings() -> Settings:
    return Settings(
        jhe_base_url="https://jhe.fly.dev",
        jhe_client_id="mcp-client",
        jhe_client_secret="mcp-secret",
        redirect_uri="unused",
        authorize_endpoint="https://jhe.fly.dev/o/authorize/",
        token_endpoint="https://jhe.fly.dev/o/token/",
        userinfo_endpoint="https://jhe.fly.dev/o/userinfo/",
        mcp_resource_url="https://jhe-mcp.fly.dev",
        broker_key="unit-test-key",
        allowed_redirects=(),
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_broker_router(_settings()))
    return TestClient(app, follow_redirects=False)


def test_protected_resource_metadata():
    r = _client().get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == "https://jhe-mcp.fly.dev"
    assert body["authorization_servers"] == ["https://jhe-mcp.fly.dev"]


def test_authorization_server_metadata():
    r = _client().get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["authorization_endpoint"] == "https://jhe-mcp.fly.dev/authorize"
    assert body["token_endpoint"] == "https://jhe-mcp.fly.dev/token"
    assert body["code_challenge_methods_supported"] == ["S256"]


def test_authorize_rejects_non_loopback_redirect():
    v = pkce.generate_verifier()
    r = _client().get(
        "/authorize",
        params={
            "response_type": "code",
            "redirect_uri": "https://evil.test/cb",
            "code_challenge": pkce.challenge_from_verifier(v),
            "code_challenge_method": "S256",
            "client_id": "llm",
        },
    )
    assert r.status_code == 400


def test_authorize_requires_pkce():
    r = _client().get(
        "/authorize",
        params={
            "response_type": "code",
            "redirect_uri": "http://localhost:9999/cb",
            "client_id": "llm",
        },
    )
    assert r.status_code == 400


def test_authorize_redirects_to_jhe_with_our_client():
    v = pkce.generate_verifier()
    r = _client().get(
        "/authorize",
        params={
            "response_type": "code",
            "redirect_uri": "http://localhost:9999/cb",
            "code_challenge": pkce.challenge_from_verifier(v),
            "code_challenge_method": "S256",
            "client_id": "llm",
            "state": "llm-state-123",
        },
    )
    assert r.status_code == 302
    loc = urllib.parse.urlparse(r.headers["location"])
    q = urllib.parse.parse_qs(loc.query)
    assert loc.netloc == "jhe.fly.dev"
    assert q["client_id"] == ["mcp-client"]
    assert q["redirect_uri"] == ["https://jhe-mcp.fly.dev/oauth/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"][0] != "llm-state-123"


@respx.mock
def test_callback_exchanges_and_redirects_back():
    respx.post("https://jhe.fly.dev/o/token/").mock(
        return_value=httpx.Response(200, json={"access_token": "JHE-TOK", "token_type": "Bearer"})
    )
    s = _settings()
    state = broker_state.encode(
        s.broker_key,
        {
            "llm_redirect_uri": "http://localhost:9999/cb",
            "llm_state": "llm-state-123",
            "llm_code_challenge": "chal",
            "llm_client_id": "llm",
            "up_verifier": pkce.generate_verifier(),
        },
    )
    r = _client().get("/oauth/callback", params={"code": "JHE-CODE", "state": state})
    assert r.status_code == 302
    loc = urllib.parse.urlparse(r.headers["location"])
    q = urllib.parse.parse_qs(loc.query)
    assert loc.netloc == "localhost:9999"
    assert q["state"] == ["llm-state-123"]
    assert "code" in q


def test_token_authorization_code_returns_jhe_token():
    s = _settings()
    v = pkce.generate_verifier()
    mc = broker_state.encode(
        s.broker_key,
        {
            "token": {"access_token": "JHE-TOK", "token_type": "Bearer"},
            "llm_code_challenge": pkce.challenge_from_verifier(v),
            "llm_redirect_uri": "http://localhost:9999/cb",
            "llm_client_id": "llm",
        },
    )
    r = _client().post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": mc,
            "code_verifier": v,
            "redirect_uri": "http://localhost:9999/cb",
            "client_id": "llm",
        },
    )
    assert r.status_code == 200
    assert r.json()["access_token"] == "JHE-TOK"


def test_token_rejects_bad_verifier():
    s = _settings()
    v = pkce.generate_verifier()
    mc = broker_state.encode(
        s.broker_key,
        {
            "token": {"access_token": "JHE-TOK"},
            "llm_code_challenge": pkce.challenge_from_verifier(v),
            "llm_redirect_uri": "http://localhost:9999/cb",
            "llm_client_id": "llm",
        },
    )
    r = _client().post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": mc,
            "code_verifier": "WRONG",
            "redirect_uri": "http://localhost:9999/cb",
            "client_id": "llm",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_authorize_rejects_userinfo_in_redirect():
    v = pkce.generate_verifier()
    r = _client().get(
        "/authorize",
        params={
            "response_type": "code",
            "redirect_uri": "https://evil.com@localhost/cb",
            "code_challenge": pkce.challenge_from_verifier(v),
            "code_challenge_method": "S256",
            "client_id": "llm",
        },
    )
    assert r.status_code == 400


def test_missing_broker_key_raises():
    s = dataclasses.replace(_settings(), broker_key=None)
    with pytest.raises(RuntimeError, match="MCP_BROKER_KEY"):
        build_broker_router(s)


def test_token_unsupported_grant_type():
    r = _client().post("/token", data={"grant_type": "client_credentials"})
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_token_rejects_client_id_mismatch():
    s = _settings()
    v = pkce.generate_verifier()
    mc = broker_state.encode(
        s.broker_key,
        {
            "token": {"access_token": "JHE-TOK"},
            "llm_code_challenge": pkce.challenge_from_verifier(v),
            "llm_redirect_uri": "http://localhost:9999/cb",
            "llm_client_id": "llm",
        },
    )
    r = _client().post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": mc,
            "code_verifier": v,
            "redirect_uri": "http://localhost:9999/cb",
            "client_id": "different-client",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_token_rejects_redirect_uri_mismatch():
    s = _settings()
    v = pkce.generate_verifier()
    mc = broker_state.encode(
        s.broker_key,
        {
            "token": {"access_token": "JHE-TOK"},
            "llm_code_challenge": pkce.challenge_from_verifier(v),
            "llm_redirect_uri": "http://localhost:9999/cb",
            "llm_client_id": "llm",
        },
    )
    r = _client().post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": mc,
            "code_verifier": v,
            "redirect_uri": "http://localhost:1111/different",
            "client_id": "llm",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


@respx.mock
def test_callback_returns_502_on_upstream_error():
    respx.post("https://jhe.fly.dev/o/token/").mock(return_value=httpx.Response(400, text="bad"))
    s = _settings()
    state = broker_state.encode(
        s.broker_key,
        {
            "llm_redirect_uri": "http://localhost:9999/cb",
            "llm_state": None,
            "llm_code_challenge": "chal",
            "llm_client_id": "llm",
            "up_verifier": pkce.generate_verifier(),
        },
    )
    r = _client().get("/oauth/callback", params={"code": "JHE-CODE", "state": state})
    assert r.status_code == 502


@respx.mock
def test_token_refresh_proxies_to_jhe():
    respx.post("https://jhe.fly.dev/o/token/").mock(
        return_value=httpx.Response(200, json={"access_token": "NEW-TOK", "token_type": "Bearer"})
    )
    r = _client().post("/token", data={"grant_type": "refresh_token", "refresh_token": "rt"})
    assert r.status_code == 200
    assert r.json()["access_token"] == "NEW-TOK"


def test_build_app_serves_metadata_and_401_header(monkeypatch):
    monkeypatch.setenv("JHE_BASE_URL", "https://jhe.fly.dev")
    monkeypatch.setenv("JHE_CLIENT_ID", "mcp-client")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://jhe-mcp.fly.dev")
    monkeypatch.setenv("MCP_BROKER_KEY", "unit-test-key")
    from jhe_mcp.config import Settings
    from jhe_mcp.server_http import build_app

    app = build_app(Settings.from_env())
    client = TestClient(app, follow_redirects=False)

    # discovery is served by the mounted broker router
    assert client.get("/.well-known/oauth-protected-resource").status_code == 200

    # unauthenticated SSE returns 401 with a resource_metadata pointer
    r = client.get("/sse")
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")
