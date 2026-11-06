"""Just enough FHIR REST for a patient pull: bearer GETs against one server, with the token's
expiry checked before every request and the token never sent anywhere else.

"Anywhere else" is a different origin: scheme, host and port must all match the server's. The
path may differ (some servers page through another base path on the same host). The check is
inside `get` and `stream`, so no caller can skip it; a `next` link or a document URL on another
origin raises `OutsideServer` before any request is made. HAPI FHIR's client and SMART's
client-js follow such links with the token attached; we hold a patient's token unattended on a
server, so we do not. Redirects are still followed: `requests` drops the Authorization header
when a redirect leaves the host or downgrades to http (a document handed off to a signed storage
URL arrives without our token going along).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests
from pydantic import SecretStr

from .http import session as http_session
from .lease import Lease
from .source_settings import FetchProfile

EXPIRY_MARGIN = timedelta(seconds=30)


class TokenExpired(Exception):
    """The EHR access token is about to expire; no further request is made."""


class Unauthorized(Exception):
    """The server answered 401: the token is gone and nothing else will work."""


class OutsideServer(Exception):
    """A URL on another origin than the server's. Never requested: the token would go with it."""


def origin(url: str) -> tuple[str, str, int | None]:
    """Scheme, host and port, with the scheme's default port made explicit."""
    parts = urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


class FhirClient:
    def __init__(
        self,
        base: str,
        token: SecretStr,
        profile: FetchProfile,
        expires_at: datetime | None,
        clock: Callable[[], datetime],
        http: requests.Session | None = None,
        lease: Lease | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.profile = profile
        self.expires_at = expires_at
        self.clock = clock
        self.http = http or http_session(profile.max_retries)
        self.lease = lease
        self._origin = origin(self.base)
        self._auth = {"Authorization": f"Bearer {token.get_secret_value()}"}
        self.requests = 0

    def ensure_valid(self) -> None:
        if self.lease is not None:
            self.lease.check()
        if self.expires_at is not None and self.clock() >= self.expires_at - EXPIRY_MARGIN:
            raise TokenExpired()

    def absolute(self, url: str) -> str | None:
        """`url` made absolute against the server, or None when it is on another origin."""
        full = url if "://" in url else f"{self.base}/{url.lstrip('/')}"
        return full if origin(full) == self._origin else None

    def _checked(self, url: str) -> str:
        full = self.absolute(url)
        if full is None:
            raise OutsideServer()
        return full

    def get(self, url: str, params: Mapping[str, str] | None = None) -> requests.Response:
        full = self._checked(url)
        self.ensure_valid()
        self.requests += 1
        r = self.http.get(
            full,
            params=params,
            headers={**self._auth, "Accept": "application/fhir+json"},
            timeout=self.profile.timeout_seconds,
        )
        if r.status_code == 401:
            raise Unauthorized()
        return r

    def stream(self, url: str, accept: str) -> requests.Response:
        """A streamed GET for document content. The caller closes the response."""
        full = self._checked(url)
        self.ensure_valid()
        self.requests += 1
        r = self.http.get(
            full,
            headers={**self._auth, "Accept": accept},
            timeout=self.profile.timeout_seconds,
            stream=True,
        )
        if r.status_code == 401:
            r.close()
            raise Unauthorized()
        return r
