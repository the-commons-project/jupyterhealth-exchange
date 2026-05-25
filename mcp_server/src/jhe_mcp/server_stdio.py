from __future__ import annotations

import time

from jhe_mcp.auth.context import AuthContext, current_auth, set_current_auth
from jhe_mcp.auth.oauth_flow import refresh_access_token, run_pkce_flow
from jhe_mcp.auth.token_cache import CachedToken, TokenCache, TokenCacheMiss
from jhe_mcp.config import Settings
from jhe_mcp.core import build_server


async def _load_or_refresh_token(settings: Settings, cache: TokenCache) -> CachedToken:
    try:
        token = cache.load()
    except TokenCacheMiss:
        return await run_pkce_flow(settings, cache)
    if not cache.needs_refresh(token):
        return token
    if not token.refresh_token:
        return await run_pkce_flow(settings, cache)
    try:
        refreshed = await refresh_access_token(
            token_endpoint=settings.token_endpoint,
            client_id=settings.jhe_client_id,
            client_secret=settings.jhe_client_secret,
            refresh_token=token.refresh_token,
        )
    except Exception:
        cache.clear()
        return await run_pkce_flow(settings, cache)
    new_token = CachedToken(
        access_token=refreshed["access_token"],
        refresh_token=refreshed.get("refresh_token") or token.refresh_token,
        expires_at=int(time.time()) + int(refreshed.get("expires_in", 3600)),
    )
    cache.save(new_token)
    return new_token


def main() -> None:
    settings = Settings.from_env()
    cache = TokenCache.default()

    async def ensure_auth() -> None:
        ctx = current_auth()
        if ctx is not None and ctx.expires_at > time.time() + 60:
            return
        token = await _load_or_refresh_token(settings, cache)
        set_current_auth(
            AuthContext(
                bearer_token=token.access_token,
                subject="local-stdio-user",
                expires_at=token.expires_at,
            )
        )

    mcp = build_server(settings, pre_tool_hook=ensure_auth)
    mcp.run()


if __name__ == "__main__":
    main()
