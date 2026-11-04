"""Phase 3: the SMART App Launch standalone flow, server side.

Discovery and PKCE are adapted from jupyterhealth/jupyter-smart-on-fhir auth.py (BSD-3). The
server holds state, verifier and tokens; the browser only ever sees the authorize redirect.

Client authentication: the registration lists methods in preference order; the launch takes the
first the deployment's Keyring can satisfy and records it on the attempt; the token exchange
authenticates the same way. Discovery is a check, not the decider: a method the server does not
list is logged and tried anyway.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from pydantic import SecretStr

from .credentials import AuthMethod, AuthMethodName, CredentialError, Keyring
from .models import A, AuthAttempt, JsonObject, TokenResponse, Value
from .source_settings import ClientRegistration

log = logging.getLogger(__name__)

ATTEMPT_TTL = timedelta(minutes=10)
ASSERTION_TTL_SECONDS = 300  # SMART: a client assertion's exp is at most 5 minutes out
DISCOVERY_TTL_SECONDS = 24 * 3600
DEFAULT_TIMEOUT = 30.0


class SmartError(Exception):
    """Phase 3 failed. The message is safe to show the patient."""


class SmartConfiguration(Value):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    code_challenge_methods: frozenset[str] = frozenset()
    auth_methods: frozenset[str] = frozenset()
    signing_algs: frozenset[str] = frozenset()

    @property
    def pkce_s256(self) -> bool:
        return "S256" in self.code_challenge_methods

    @classmethod
    def parse(cls, iss: str, raw: JsonObject) -> SmartConfiguration:
        def strings(key: str) -> frozenset[str]:
            v = raw.get(key)
            return frozenset(x for x in v if isinstance(x, str)) if isinstance(v, list) else frozenset()

        return cls(
            issuer=iss,
            authorization_endpoint=str(raw["authorization_endpoint"]),
            token_endpoint=str(raw["token_endpoint"]),
            code_challenge_methods=strings("code_challenge_methods_supported"),
            auth_methods=strings("token_endpoint_auth_methods_supported"),
            signing_algs=strings("token_endpoint_auth_signing_alg_values_supported"),
        )


_cache: dict[str, tuple[float, SmartConfiguration]] = {}
_cache_lock = threading.Lock()


def discover(iss: str, http: requests.Session, timeout: float = DEFAULT_TIMEOUT) -> SmartConfiguration:
    """`.well-known/smart-configuration`, cached per base URL for a day."""
    key = iss.rstrip("/")
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < DISCOVERY_TTL_SECONDS:
        return hit[1]
    # Without the trailing slash: some servers send iss with one and refuse the double slash.
    try:
        r = http.get(f"{key}/.well-known/smart-configuration", headers={"Accept": "application/json"}, timeout=timeout)
        r.raise_for_status()
        config = SmartConfiguration.parse(iss, r.json())
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        raise SmartError(f"Could not reach this organization's sign-in service ({type(e).__name__}).") from e
    with _cache_lock:
        _cache[key] = (now, config)
    return config


def clear_discovery_cache() -> None:
    with _cache_lock:
        _cache.clear()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def choose_method(registration: ClientRegistration, keyring: Keyring, config: SmartConfiguration) -> AuthMethod:
    """The first method the deployment can satisfy, checked against what the server lists."""
    try:
        method = keyring.choose(registration.methods)
    except CredentialError as e:
        log.error("vendor %s: %s", registration.vendor, e)
        raise SmartError("This service is not set up to connect to this organization yet.") from e
    if config.auth_methods and method.method is not AuthMethodName.PUBLIC and method.method not in config.auth_methods:
        log.warning("%s does not list %s; trying it anyway", config.issuer, method.method)
    if (
        method.method is AuthMethodName.PRIVATE_KEY_JWT
        and config.signing_algs
        and method.alg not in config.signing_algs
    ):
        log.error("%s accepts %s for client assertions, not %s", config.issuer, sorted(config.signing_algs), method.alg)
        raise SmartError("This organization does not accept this service's sign-in key.")
    return method


def authorize_url(
    config: SmartConfiguration, registration: ClientRegistration, attempt: AuthAttempt[A], challenge: str
) -> str:
    params = {
        "response_type": "code",
        "client_id": registration.client_id,
        "redirect_uri": attempt.redirect_uri,
        "scope": " ".join(attempt.scopes),
        "state": attempt.state,
        "aud": attempt.selection.endpoint.iss,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{config.authorization_endpoint}?{urlencode(params)}"


def assertion_claims(client_id: str, token_endpoint: str, now: int) -> dict[str, str | int]:
    """private_key_jwt per SMART: iss = sub = client id, aud = token endpoint, unique jti."""
    return {
        "iss": client_id,
        "sub": client_id,
        "aud": token_endpoint,
        "jti": secrets.token_urlsafe(24),
        "iat": now,
        "exp": now + ASSERTION_TTL_SECONDS,
    }


def _recorded_method(attempt: AuthAttempt[A], registration: ClientRegistration, keyring: Keyring) -> AuthMethod:
    """The method chosen at launch, as the registration now describes it."""
    for m in registration.methods:
        if m.method == attempt.auth_method and m.kid == attempt.key_id:
            return m
    log.warning("vendor %s: launch method no longer configured; choosing again", registration.vendor)
    return keyring.choose(registration.methods)


def _client_auth(
    attempt: AuthAttempt[A], registration: ClientRegistration, keyring: Keyring
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """The form fields and basic-auth pair that authenticate this client."""
    method = _recorded_method(attempt, registration, keyring)
    match method.method:
        case AuthMethodName.PUBLIC:
            return {"client_id": registration.client_id}, None
        case AuthMethodName.CLIENT_SECRET_BASIC:
            return {}, (registration.client_id, keyring.secrets.get(method.secret or ""))
        case AuthMethodName.CLIENT_SECRET_POST:
            return {
                "client_id": registration.client_id,
                "client_secret": keyring.secrets.get(method.secret or ""),
            }, None
        case AuthMethodName.PRIVATE_KEY_JWT:
            claims = assertion_claims(registration.client_id, attempt.token_endpoint, int(time.time()))
            return {
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": keyring.keys.sign(method.kid or "", claims, method.alg),
            }, None


def exchange(
    attempt: AuthAttempt[A],
    code: str,
    registration: ClientRegistration,
    keyring: Keyring,
    http: requests.Session,
    clock: Callable[[], datetime],
    timeout: float = DEFAULT_TIMEOUT,
) -> TokenResponse:
    """POST the code with the PKCE verifier and the client authentication chosen at launch."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": attempt.redirect_uri,
        "code_verifier": attempt.code_verifier.get_secret_value(),
    }
    try:
        fields, auth = _client_auth(attempt, registration, keyring)
    except CredentialError as e:
        log.error("vendor %s: %s", registration.vendor, e)
        raise SmartError("This service is not set up to connect to this organization yet.") from e
    try:
        r = http.post(
            attempt.token_endpoint,
            data={**data, **fields},
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise SmartError(f"The organization's sign-in service did not respond ({type(e).__name__}).") from e
    if r.status_code != 200:
        raise SmartError(f"The organization refused the sign-in ({r.status_code}).")
    try:
        body = r.json()
    except ValueError:
        raise SmartError("The organization's sign-in service sent an unreadable answer.") from None
    access_token, patient = body.get("access_token"), body.get("patient")
    if not isinstance(access_token, str) or not access_token or patient in (None, ""):
        raise SmartError("The organization did not return patient access.")
    scope = body.get("scope")
    granted = tuple((scope if isinstance(scope, str) else " ".join(attempt.scopes)).split())
    expires_in = body.get("expires_in")
    seconds = int(expires_in) if isinstance(expires_in, int | float | str) and str(expires_in).isdigit() else 3600
    # Refresh and id tokens are dropped here: offline access is not requested, and a token we do
    # not use must not be stored or logged.
    return TokenResponse(
        access_token=SecretStr(access_token),
        expires_at=clock() + timedelta(seconds=seconds),
        scopes_granted=granted,
        patient=str(patient),
        token_type=str(body.get("token_type") or "Bearer"),
        refresh_token_issued="refresh_token" in body,
    )
