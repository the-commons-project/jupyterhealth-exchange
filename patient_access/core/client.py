"""Just enough FHIR REST for a patient pull: bearer GETs against one server, with the token's
expiry checked before every request and the token never sent to another host."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

import requests
from pydantic import SecretStr

from .http import session as http_session
from .source_settings import FetchProfile

EXPIRY_MARGIN = timedelta(seconds=30)


class TokenExpired(Exception):
    """The EHR access token is about to expire; no further request is made."""


class Unauthorized(Exception):
    """The server answered 401: the token is gone and nothing else will work."""


class FhirClient:
    def __init__(
        self,
        base: str,
        token: SecretStr,
        profile: FetchProfile,
        expires_at: datetime | None,
        clock: Callable[[], datetime],
        http: requests.Session | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.profile = profile
        self.expires_at = expires_at
        self.clock = clock
        self.http = http or http_session(profile.max_retries)
        self._auth = {"Authorization": f"Bearer {token.get_secret_value()}"}
        self.requests = 0

    def ensure_valid(self) -> None:
        if self.expires_at is not None and self.clock() >= self.expires_at - EXPIRY_MARGIN:
            raise TokenExpired()

    def absolute(self, url: str) -> str | None:
        """`url` made absolute against the server, or None when it points at another host."""
        if "://" not in url:
            return f"{self.base}/{url.lstrip('/')}"
        return url if url == self.base or url.startswith(self.base + "/") else None

    def get(self, url: str, params: Mapping[str, str] | None = None) -> requests.Response:
        self.ensure_valid()
        self.requests += 1
        r = self.http.get(
            url,
            params=params,
            headers={**self._auth, "Accept": "application/fhir+json"},
            timeout=self.profile.timeout_seconds,
        )
        if r.status_code == 401:
            raise Unauthorized()
        return r

    def stream(self, url: str, accept: str) -> requests.Response:
        """A streamed GET for document content. The caller closes the response."""
        self.ensure_valid()
        self.requests += 1
        r = self.http.get(
            url, headers={**self._auth, "Accept": accept}, timeout=self.profile.timeout_seconds, stream=True
        )
        if r.status_code == 401:
            r.close()
            raise Unauthorized()
        return r
