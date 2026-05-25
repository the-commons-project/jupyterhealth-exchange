"""Validate an opaque OAuth bearer token by calling JHE's OIDC userinfo endpoint.

JHE (django-oauth-toolkit) issues opaque access tokens by default, not JWTs.
Calling `/o/userinfo/` with `Authorization: Bearer <opaque>` returns 200 + the
`sub` claim when the token is valid, and 401 otherwise — the standard OIDC
pattern for opaque-token validation.

We cache results for `cache_ttl` seconds so repeated MCP requests from the
same client don't hammer JHE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


class TokenValidationError(Exception):
    pass


@dataclass(frozen=True)
class _CachedSub:
    subject: str
    cached_at: float


class UserinfoValidator:
    def __init__(self, userinfo_endpoint: str, cache_ttl: int = 60, timeout: float = 5.0) -> None:
        self._endpoint = userinfo_endpoint
        self._cache_ttl = cache_ttl
        self._timeout = timeout
        self._cache: dict[str, _CachedSub] = {}

    async def verify(self, token: str) -> str:
        """Return the `sub` claim if `token` is valid; raise on failure."""
        now = time.time()
        cached = self._cache.get(token)
        if cached is not None and now - cached.cached_at < self._cache_ttl:
            return cached.subject
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(self._endpoint, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 401:
            self._cache.pop(token, None)
            raise TokenValidationError("token rejected by userinfo endpoint")
        if resp.status_code != 200:
            raise TokenValidationError(f"userinfo returned {resp.status_code}: {resp.text}")
        sub = resp.json().get("sub")
        if not sub:
            raise TokenValidationError("userinfo response missing 'sub' claim")
        self._cache[token] = _CachedSub(subject=sub, cached_at=now)
        return sub
