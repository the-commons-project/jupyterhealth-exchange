from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import httpx

from jhe_mcp.auth.token_cache import CachedToken, TokenCache
from jhe_mcp.config import Settings


@dataclass(frozen=True)
class PkcePair:
    code_verifier: str
    code_challenge: str


def generate_pkce_pair() -> PkcePair:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkcePair(code_verifier=verifier, code_challenge=challenge)


def build_authorize_url(
    *,
    authorize_endpoint: str,
    client_id: str,
    redirect_uri: str,
    pkce: PkcePair,
    state: str,
    scope: str = "openid",
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{authorize_endpoint}?{urllib.parse.urlencode(params)}"


async def _post_token_endpoint(
    token_endpoint: str,
    client_id: str,
    client_secret: str | None,
    grant_fields: dict[str, str],
    timeout: float = 10.0,
) -> dict[str, Any]:
    data = {"client_id": client_id, **grant_fields}
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(token_endpoint, data=data)
        resp.raise_for_status()
        return resp.json()


async def exchange_code_for_tokens(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str | None,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return await _post_token_endpoint(
        token_endpoint,
        client_id,
        client_secret,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout,
    )


async def refresh_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return await _post_token_endpoint(
        token_endpoint,
        client_id,
        client_secret,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout,
    )


def _start_callback_listener(redirect_uri: str) -> tuple[threading.Thread, dict[str, str]]:
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8765
    received: dict[str, str] = {}
    completed = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            # Ignore browser preflight chatter (favicon, prefetch) — they would
            # otherwise complete the listener before the real ?code= callback.
            qs = urllib.parse.urlparse(self.path).query
            parsed = dict(urllib.parse.parse_qsl(qs))
            if "code" not in parsed and "error" not in parsed:
                self.send_response(404)
                self.end_headers()
                return
            received.update(parsed)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>JHE login complete</h1>You may close this window.</body></html>")
            completed.set()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer((host, port), Handler)

    def serve() -> None:
        while not completed.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, received


async def run_pkce_flow(settings: Settings, cache: TokenCache) -> CachedToken:
    pair = generate_pkce_pair()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(
        authorize_endpoint=settings.authorize_endpoint,
        client_id=settings.jhe_client_id,
        redirect_uri=settings.redirect_uri,
        pkce=pair,
        state=state,
    )
    _, received = _start_callback_listener(settings.redirect_uri)
    webbrowser.open(url)
    deadline = time.time() + 300
    while "code" not in received and time.time() < deadline:
        await asyncio.sleep(0.5)
    if "code" not in received:
        raise RuntimeError("Timed out waiting for OAuth callback")
    if received.get("state") != state:
        raise RuntimeError("State mismatch in OAuth callback")
    tokens = await exchange_code_for_tokens(
        token_endpoint=settings.token_endpoint,
        client_id=settings.jhe_client_id,
        client_secret=settings.jhe_client_secret,
        code=received["code"],
        redirect_uri=settings.redirect_uri,
        code_verifier=pair.code_verifier,
    )
    cached = CachedToken(
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        expires_at=int(time.time()) + int(tokens.get("expires_in", 3600)),
    )
    cache.save(cached)
    return cached
