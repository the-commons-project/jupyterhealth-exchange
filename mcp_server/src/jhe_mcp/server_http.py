from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jhe_mcp.auth.broker import build_broker_router
from jhe_mcp.auth.context import AuthContext, set_current_auth
from jhe_mcp.auth.userinfo import TokenValidationError, UserinfoValidator
from jhe_mcp.config import Settings
from jhe_mcp.core import build_server

logger = logging.getLogger(__name__)


def build_app(settings: Settings) -> FastAPI:
    mcp = build_server(settings)
    validator = UserinfoValidator(userinfo_endpoint=settings.userinfo_endpoint)
    challenge = f'Bearer resource_metadata="{settings.mcp_resource_url}/.well-known/oauth-protected-resource"'

    streamable_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="jhe-mcp HTTP", lifespan=lifespan)
    app.include_router(build_broker_router(settings))

    # The Streamable HTTP app serves the MCP endpoint at /mcp.
    AUTHED_PREFIXES = ("/mcp",)

    @app.middleware("http")
    async def attach_auth(request: Request, call_next):
        if not any(request.url.path.startswith(p) for p in AUTHED_PREFIXES):
            return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "missing bearer token"},
                headers={"WWW-Authenticate": challenge},
            )
        token = auth_header.split(" ", 1)[1].strip()
        try:
            subject = await validator.verify(token)
        except TokenValidationError as exc:
            logger.warning("Token validation failed: %s", exc)
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or expired token"},
                headers={"WWW-Authenticate": challenge},
            )
        # expires_at is unused in HTTP/broker mode: token revalidation is governed by
        # UserinfoValidator's cache TTL, not this field. 0 = "not applicable here".
        ctx_token = set_current_auth(AuthContext(bearer_token=token, subject=subject, expires_at=0))
        try:
            return await call_next(request)
        finally:
            from jhe_mcp.auth.context import _current

            _current.reset(ctx_token)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.mount("/", streamable_app)
    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    app = build_app(settings)
    port = int(os.environ.get("MCP_HTTP_PORT", "8401"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
