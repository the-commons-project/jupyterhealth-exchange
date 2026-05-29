from __future__ import annotations

import logging
import urllib.parse

import httpx

logger = logging.getLogger(__name__)
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from jhe_mcp.auth import broker_state, pkce
from jhe_mcp.config import Settings

STATE_TTL = 600  # seconds; authorize -> callback
CODE_TTL = 30  # seconds; callback -> token. Short window because the stateless
# authorization code is single-use only by TTL (no server-side consumption record),
# so we minimize the replay window.


def _is_allowed_redirect(uri: str, allowed: tuple[str, ...]) -> bool:
    if uri in allowed:
        return True
    try:
        parsed = urllib.parse.urlparse(uri)
    except ValueError:
        return False
    if parsed.username or parsed.password:  # reject userinfo open-redirect tricks
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return parsed.hostname in ("localhost", "127.0.0.1", "::1")


def build_broker_router(settings: Settings) -> APIRouter:
    if not settings.broker_key:
        raise RuntimeError("MCP_BROKER_KEY is required to run the OAuth broker")

    router = APIRouter()
    base = settings.mcp_resource_url
    callback_uri = f"{base}/oauth/callback"

    @router.get("/.well-known/oauth-protected-resource")
    async def protected_resource() -> dict:
        return {
            "resource": base,
            "authorization_servers": [base],
            "scopes_supported": ["openid", "email"],
            "bearer_methods_supported": ["header"],
        }

    @router.get("/.well-known/oauth-authorization-server")
    async def authorization_server() -> dict:
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }

    @router.get("/authorize")
    async def authorize(request: Request):
        q = request.query_params
        if q.get("response_type") != "code":
            return PlainTextResponse("unsupported_response_type", status_code=400)
        redirect_uri = q.get("redirect_uri", "")
        if not _is_allowed_redirect(redirect_uri, settings.allowed_redirects):
            return PlainTextResponse("invalid redirect_uri", status_code=400)
        challenge = q.get("code_challenge")
        if not challenge or q.get("code_challenge_method", "S256") != "S256":
            return PlainTextResponse("PKCE S256 required", status_code=400)

        up_verifier = pkce.generate_verifier()
        state = broker_state.encode(
            settings.broker_key,
            {
                "llm_redirect_uri": redirect_uri,
                "llm_state": q.get("state"),
                "llm_code_challenge": challenge,
                # client_id is intentionally not validated against a fixed value: for public
                # PKCE clients the security boundary is PKCE + the loopback/allow-listed
                # redirect_uri, not a shared static client_id. /token re-checks that the
                # client_id presented there matches the one bound into the code here.
                "llm_client_id": q.get("client_id"),
                "up_verifier": up_verifier,
            },
        )
        params = {
            "response_type": "code",
            "client_id": settings.jhe_client_id,
            "redirect_uri": callback_uri,
            "scope": q.get("scope", "openid email"),
            "code_challenge": pkce.challenge_from_verifier(up_verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
        url = f"{settings.authorize_endpoint}?{urllib.parse.urlencode(params)}"
        return RedirectResponse(url, status_code=302)

    @router.get("/oauth/callback")
    async def oauth_callback(request: Request):
        q = request.query_params
        try:
            st = broker_state.decode(settings.broker_key, q.get("state", ""), STATE_TTL)
        except broker_state.StateError:
            return PlainTextResponse("invalid state", status_code=400)

        data = {
            "grant_type": "authorization_code",
            "code": q.get("code", ""),
            "redirect_uri": callback_uri,
            "client_id": settings.jhe_client_id,
            "code_verifier": st["up_verifier"],
        }
        if settings.jhe_client_secret:
            data["client_secret"] = settings.jhe_client_secret
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(settings.token_endpoint, data=data)
        except httpx.HTTPError:
            logger.error("JHE token exchange transport error")
            return PlainTextResponse("upstream unavailable", status_code=503)
        if resp.status_code != 200:
            logger.error("JHE token exchange failed: %s %s", resp.status_code, resp.text)
            return PlainTextResponse("upstream error", status_code=502)
        try:
            jhe_token = resp.json()
        except ValueError:
            logger.error("JHE token endpoint returned non-JSON")
            return PlainTextResponse("upstream error", status_code=502)

        mc = broker_state.encode(
            settings.broker_key,
            {
                "token": jhe_token,
                "llm_code_challenge": st["llm_code_challenge"],
                "llm_redirect_uri": st["llm_redirect_uri"],
                "llm_client_id": st["llm_client_id"],
            },
        )
        out_params = {"code": mc}
        if st.get("llm_state"):
            out_params["state"] = st["llm_state"]
        sep = "&" if "?" in st["llm_redirect_uri"] else "?"
        location = f"{st['llm_redirect_uri']}{sep}{urllib.parse.urlencode(out_params)}"
        return RedirectResponse(location, status_code=302)

    @router.post("/token")
    async def token(
        grant_type: str = Form(...),
        code: str | None = Form(None),
        code_verifier: str | None = Form(None),
        redirect_uri: str | None = Form(None),
        client_id: str | None = Form(None),
        refresh_token: str | None = Form(None),
    ):
        if grant_type == "authorization_code":
            try:
                blob = broker_state.decode(settings.broker_key, code or "", CODE_TTL)
            except broker_state.StateError:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if not code_verifier or not pkce.verify(code_verifier, blob["llm_code_challenge"]):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if client_id != blob.get("llm_client_id"):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if redirect_uri != blob["llm_redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(blob["token"])

        if grant_type == "refresh_token":
            data = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token or "",
                "client_id": settings.jhe_client_id,
            }
            if settings.jhe_client_secret:
                data["client_secret"] = settings.jhe_client_secret
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(settings.token_endpoint, data=data)
            except httpx.HTTPError:
                return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
            if resp.status_code != 200:
                logger.error("JHE refresh_token exchange failed: %s", resp.status_code)
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            try:
                body = resp.json()
            except ValueError:
                return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
            return JSONResponse(body, status_code=200)

        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    return router
