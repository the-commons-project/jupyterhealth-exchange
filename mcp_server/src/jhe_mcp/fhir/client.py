from __future__ import annotations

import asyncio
from typing import Any

import httpx

from jhe_mcp.auth.context import current_auth_required


class JheClientError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"JHE {status}: {body}")
        self.status = status
        self.body = body


class JheClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> JheClient:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def admin_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        treat_404_as_none: bool = False,
    ) -> Any:
        return await self._get(f"/api/v1/{path.lstrip('/')}", params, treat_404_as_none)

    async def fhir_get(
        self,
        resource_path: str,
        params: dict[str, Any] | None = None,
        treat_404_as_none: bool = False,
    ) -> Any:
        return await self._get(f"/fhir/r5/{resource_path.lstrip('/')}", params, treat_404_as_none)

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None,
        treat_404_as_none: bool,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("JheClient must be used as async context manager")
        ctx = current_auth_required()
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {ctx.bearer_token}"}
        # One retry on transport errors or 5xx; everything else returns/raises immediately.
        for is_retry in (False, True):
            try:
                resp = await self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if is_retry:
                    raise JheClientError(0, str(exc)) from exc
                await asyncio.sleep(0.5)
                continue
            if resp.status_code == 404 and treat_404_as_none:
                return None
            if 500 <= resp.status_code < 600 and not is_retry:
                await asyncio.sleep(0.5)
                continue
            if resp.status_code >= 400:
                raise JheClientError(resp.status_code, resp.text)
            return resp.json()
        raise AssertionError("unreachable: retry loop must return or raise")  # pragma: no cover
