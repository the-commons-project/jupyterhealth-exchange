"""Just enough FHIR REST for a patient pull: bearer GETs against one server, with the token's
expiry checked before every request, and one policy for every URL the EHR hands us.

The EHR is trusted to say where its data is. A `next` link or an attachment URL on another host is
fetched with the token, as US Core asks ("hosted on a server outside the FHIR server, it SHOULD be
securely accessible using the same authorization credentials") and as SMART's client-js and HAPI
do. What the EHR did not say is not trusted: redirects are followed by this client itself, and the
token does not cross a change of host or an https-to-http downgrade (HAPI Core's CVE-2026-34359 is
exactly that leak). Every URL, first request or redirect, passes `UrlPolicy`: it must parse, it
must be https unless the dev switch allows http, and it must not name a private, loopback or
link-local address, since this server follows URLs it did not choose. A refused URL raises
`RefusedUrl` before any request is made; the caller records that one type or document and goes on.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urljoin, urlsplit

import requests
from pydantic import SecretStr

from .http import session as http_session
from .lease import Lease
from .source_settings import FetchProfile

EXPIRY_MARGIN = timedelta(seconds=30)
MAX_REDIRECTS = 5


class TokenExpired(Exception):
    """The EHR access token is about to expire; no further request is made."""


class Unauthorized(Exception):
    """The EHR's own server answered 401 to the token: it is gone and nothing else will work."""


class UrlRefusal(StrEnum):
    UNPARSEABLE = "unparseable"
    NOT_HTTPS = "not_https"
    PRIVATE_ADDRESS = "private_address"
    TOO_MANY_REDIRECTS = "too_many_redirects"


class RefusedUrl(Exception):
    """A URL the policy will not request. `reason` is recorded on the type or the document."""

    def __init__(self, reason: UrlRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


Resolver = Callable[[str], Sequence[str]]


def resolve_host(host: str) -> Sequence[str]:
    """Every address a host name resolves to, as strings. Empty when it does not resolve: the
    connection will fail on its own, as a network error."""
    try:
        return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
    except (socket.gaierror, UnicodeError):  # no such name, or one the IDNA codec refuses
        return []


def _literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host names outright, if it is one: dotted or bracketed, or one of the
    numeric spellings `inet_aton` accepts (`127.1`, `2130706433`, `0x7f000001`)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _is_private(address: str) -> bool:
    """Anything that is not a public internet address: private, loopback, link-local, the shared
    address space, multicast, reserved, unspecified."""
    ip = _literal(address)
    return ip is not None and (not ip.is_global or ip.is_multicast)


def origin(url: str) -> tuple[str, str, int | None]:
    """Scheme, host and port, with the scheme's default port made explicit. Raises ValueError
    for a URL whose port cannot be read."""
    parts = urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


@dataclass(frozen=True)
class UrlPolicy:
    """What an EHR-given URL must satisfy before it is requested. `allow_http` is the dev switch
    for sandboxes on localhost: it also turns the private-address check off, since local is the
    point. `resolver` is how host names are checked against private ranges; None checks literal
    addresses only (a hostname is then left to the network)."""

    allow_http: bool = False
    resolver: Resolver | None = None

    def check(self, url: str) -> str:
        """`url`, or `RefusedUrl`."""
        try:
            scheme, host, _ = origin(url)
        except ValueError:
            raise RefusedUrl(UrlRefusal.UNPARSEABLE) from None
        if not host:
            raise RefusedUrl(UrlRefusal.UNPARSEABLE)
        if scheme != "https" and not (scheme == "http" and self.allow_http):
            raise RefusedUrl(UrlRefusal.NOT_HTTPS)
        if not self.allow_http:
            addresses = (
                [host]
                if _is_private(host) or host == "localhost"
                else list(self.resolver(host) if self.resolver else [])
            )
            if host == "localhost" or any(_is_private(a) for a in addresses):
                raise RefusedUrl(UrlRefusal.PRIVATE_ADDRESS)
        return url


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
        policy: UrlPolicy = UrlPolicy(),
    ) -> None:
        self.base = base.rstrip("/")
        self.profile = profile
        self.expires_at = expires_at
        self.clock = clock
        self.http = http or http_session(profile.max_retries)
        self.lease = lease
        self.policy = policy
        self._origin = origin(self.base)
        self._auth = {"Authorization": f"Bearer {token.get_secret_value()}"}
        self.requests = 0

    def ensure_valid(self) -> None:
        if self.lease is not None:
            self.lease.check()
        if self.expires_at is not None and self.clock() >= self.expires_at - EXPIRY_MARGIN:
            raise TokenExpired()

    def absolute(self, url: str) -> str:
        """`url` made absolute against the server and checked by the policy (`RefusedUrl`)."""
        full = url if "://" in url else f"{self.base}/{url.lstrip('/')}"
        return self.policy.check(full)

    def _get(
        self, url: str, params: Mapping[str, str] | None, accept: str, stream: bool, timeout: float | None = None
    ) -> requests.Response:
        """One GET with the token, following redirects itself: each hop passes the policy, and the
        token stays behind when the host changes or https drops to http."""
        full = self.absolute(url)
        self.ensure_valid()
        headers: dict[str, str] = {**self._auth, "Accept": accept}
        for _ in range(MAX_REDIRECTS + 1):
            self.requests += 1
            r = self.http.get(
                full,
                params=params,
                headers=headers,
                timeout=timeout or self.profile.timeout_seconds,
                stream=stream,
                allow_redirects=False,
            )
            location = r.headers.get("Location")
            if not (r.is_redirect and location):
                if r.status_code == 401 and "Authorization" in headers and origin(full) == self._origin:
                    # The EHR itself refused our token: nothing else will work. A 401 from any
                    # other host (a document server, storage after a token-less hop) is that
                    # one request's answer, returned to the caller like any other status.
                    r.close()
                    raise Unauthorized()
                return r
            r.close()
            params = None  # the redirect target carries its own query
            target = self.policy.check(urljoin(full, location))
            if _credentials_cross(full, target):
                headers.pop("Authorization", None)
            full = target
        raise RefusedUrl(UrlRefusal.TOO_MANY_REDIRECTS)

    def get(self, url: str, params: Mapping[str, str] | None = None) -> requests.Response:
        return self._get(url, params, "application/fhir+json", stream=False)

    def stream(self, url: str, accept: str) -> requests.Response:
        """A streamed GET for document content. The caller closes the response."""
        return self._get(url, None, accept, stream=True, timeout=self.profile.documents.timeout_seconds)


def _credentials_cross(from_url: str, to_url: str) -> bool:
    """Whether a redirect from one URL to the other leaves the host or downgrades the scheme:
    the same rule `requests` applies, so a manual hop and an automatic one agree."""
    a, b = origin(from_url), origin(to_url)
    return a[1] != b[1] or (a[0] == "https" and b[0] == "http") or a[2] != b[2]
