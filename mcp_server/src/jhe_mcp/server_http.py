from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jhe_mcp.auth.context import AuthContext, set_current_auth
from jhe_mcp.auth.userinfo import TokenValidationError, UserinfoValidator
from jhe_mcp.config import Settings
from jhe_mcp.core import build_server


def build_app(settings: Settings) -> FastAPI:
    mcp = build_server(settings)
    validator = UserinfoValidator(userinfo_endpoint=settings.userinfo_endpoint)

    sse_app = mcp.sse_app()
    app = FastAPI(title="jhe-mcp HTTP/SSE")

    # FastMCP's SSE app serves both /sse (GET stream) and /messages (POST from client).
    AUTHED_PREFIXES = ("/sse", "/messages")

    @app.middleware("http")
    async def attach_auth(request: Request, call_next):
        if not any(request.url.path.startswith(p) for p in AUTHED_PREFIXES):
            return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(status_code=401, content={"detail": "missing bearer token"})
        token = auth_header.split(" ", 1)[1].strip()
        try:
            subject = await validator.verify(token)
        except TokenValidationError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})
        set_current_auth(AuthContext(bearer_token=token, subject=subject, expires_at=0))
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.mount("/", sse_app)
    return app


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    app = build_app(settings)
    port = int(os.environ.get("MCP_HTTP_PORT", "8401"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
