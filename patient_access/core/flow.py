"""Phase 3 as plain functions over values; every web layer is a thin adapter.

The launch is one step. The callback is three, which the host composes with its own checks in
between:

    attempt = begin_callback(state, attempts)            consumes the attempt (single use)
        host: the session's patient is the attempt's participant, else stop (the code is unspent)
    token = exchange_code(attempt, code=..., ...)        the only call to the EHR
        host, in one transaction with the next step: its own records for the pull, and the
        job's participant
    job = create_job(attempt, token, participant, ...)   the job row; dispatched after commit

`begin_callback` must commit on its own, before the session check: in a transaction with the
later steps, any later failure would roll the consume back and make the attempt usable again.
Only the host's records and `create_job` share a transaction.

Every failure raises `FlowError`, whose message is safe to show the patient and whose `kind`
says what happened.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

import requests
from pydantic import BaseModel, SecretStr

from .credentials import Keyring
from .interfaces import AuthAttemptStore, Directory, JobDispatcher, JobStore
from .log import log
from .models import AuthAttempt, Credentials, Job, Participant, Source, TokenResponse
from .puller import plan_outcomes
from .smart import ATTEMPT_TTL, SmartError, authorize_url, choose_method, discover, exchange, pkce_pair

LA = TypeVar("LA", bound=BaseModel)  # the attempt's (launch-time) attributes
JA = TypeVar("JA", bound=BaseModel)  # the job's attributes


class FlowErrorKind(StrEnum):
    UNKNOWN_SELECTION = "unknown_selection"
    NOT_CONFIGURED = "not_configured"
    SIGN_IN_UNAVAILABLE = "sign_in_unavailable"  # discovery or the token endpoint failed
    ATTEMPT_INVALID = "attempt_invalid"  # unknown, expired or used state
    DECLINED = "declined"  # the EHR returned an error, e.g. access_denied
    NO_CODE = "no_code"


class FlowError(Exception):
    def __init__(self, kind: FlowErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def start_upstream_auth(
    *,
    participant: Participant[LA],
    selection_id: str,
    scopes: tuple[str, ...],
    scopes_declined: tuple[str, ...],
    redirect_uri: str,
    directory: Directory,
    keyring: Keyring,
    attempts: AuthAttemptStore[LA],
    http: requests.Session,
    clock: Callable[[], datetime],
    allow_http: bool = False,
) -> str:
    """Discover, record the attempt, return the authorize URL to send the browser to.
    `allow_http`: accept an http authorize endpoint (development only)."""
    try:
        selection = directory.selection(selection_id)
    except KeyError:
        raise FlowError(FlowErrorKind.UNKNOWN_SELECTION, "That organization is not in the directory.") from None
    try:
        settings = directory.settings_for(selection.endpoint)
    except KeyError:
        raise FlowError(
            FlowErrorKind.NOT_CONFIGURED, f"{selection.brand.name} is not available for connection yet."
        ) from None
    try:
        config = discover(selection.endpoint.iss, http, settings.fetch.timeout_seconds, allow_http)
        method = choose_method(settings.registration, keyring, config)
    except SmartError as e:
        raise FlowError(FlowErrorKind.SIGN_IN_UNAVAILABLE, str(e)) from e
    verifier, challenge = pkce_pair()
    now = clock()
    attempt = AuthAttempt[LA](
        state=secrets.token_urlsafe(32),
        code_verifier=SecretStr(verifier),
        participant=participant,
        selection=selection,
        scopes=settings.scopes(scopes),
        scopes_declined=tuple(map(settings.registration.spelled, scopes_declined)),
        redirect_uri=redirect_uri,
        token_endpoint=config.token_endpoint,
        created_at=now,
        expires_at=now + ATTEMPT_TTL,
        auth_method=method.method,
        key_id=method.kid,
        scope_version=settings.registration.scope_version,
        pkce_advertised=config.pkce_s256,
        type_choice=settings.type_choice,
    )
    attempts.create(attempt)
    # The URL itself is never logged (its query carries the state and the PKCE challenge); what
    # it asks for is: the endpoint, the method and the exact scope string sent.
    log.info(
        "launch %s via %s (%s, scopes %s): scope=%r declined=%r",
        selection.endpoint.vendor,
        config.authorization_endpoint,
        method.method,
        settings.registration.scope_version,
        " ".join(attempt.scopes),
        " ".join(attempt.scopes_declined),
    )
    return authorize_url(config, settings.registration, attempt, challenge)


def begin_callback(state: str, attempts: AuthAttemptStore[LA]) -> AuthAttempt[LA]:
    try:
        return attempts.consume(state)
    except KeyError:
        raise FlowError(
            FlowErrorKind.ATTEMPT_INVALID,
            "This sign-in has expired or was already used. Please start again from your invitation link.",
        ) from None


def exchange_code(
    attempt: AuthAttempt[LA],
    *,
    code: str | None,
    error: str | None,
    directory: Directory,
    keyring: Keyring,
    http: requests.Session,
    clock: Callable[[], datetime],
) -> TokenResponse:
    """The EHR's answer turned into a token. `error` is the callback's `error` parameter; its
    description is not shown, since the EHR wrote it."""
    if error:
        raise FlowError(
            FlowErrorKind.DECLINED,
            "Sign-in at your healthcare organization did not complete, so nothing was shared.",
        )
    if not code:
        raise FlowError(FlowErrorKind.NO_CODE, "Your healthcare organization did not complete the sign-in.")
    try:
        settings = directory.settings_for(attempt.selection.endpoint)
    except KeyError:
        raise FlowError(
            FlowErrorKind.NOT_CONFIGURED, f"{attempt.selection.brand.name} is not available for connection yet."
        ) from None
    try:
        return exchange(attempt, code, settings.registration, keyring, http, clock, settings.fetch.timeout_seconds)
    except SmartError as e:
        raise FlowError(FlowErrorKind.SIGN_IN_UNAVAILABLE, str(e)) from e


def create_job(
    attempt: AuthAttempt[LA],
    token: TokenResponse,
    participant: Participant[JA],
    *,
    directory: Directory,
    jobs: JobStore[JA],
    dispatcher: JobDispatcher,
    clock: Callable[[], datetime],
) -> Job[JA]:
    """The job row, with the EHR token encrypted at rest by the store, and its dispatch."""
    try:
        settings = directory.settings_for(attempt.selection.endpoint)
    except KeyError:
        raise FlowError(
            FlowErrorKind.NOT_CONFIGURED, f"{attempt.selection.brand.name} is not available for connection yet."
        ) from None
    job = jobs.create(
        Job[JA](
            id="",
            participant=participant,
            source=Source.of(attempt, token),
            credentials=Credentials(ehr_access_token=token.access_token),
            token_expires_at=token.expires_at,
            outcomes=plan_outcomes(settings, attempt.scopes, token.scopes_granted),
            created_at=clock(),
        )
    )
    dispatcher.dispatch(job.id)
    return job
