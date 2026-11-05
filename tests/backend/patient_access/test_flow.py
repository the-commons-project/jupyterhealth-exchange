"""Phase 3: the launch and the three-step callback, against a fake authorization server."""

import sys
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from patient_access.core.credentials import PUBLIC_ONLY
from patient_access.core.flow import (
    FlowError,
    FlowErrorKind,
    begin_callback,
    create_job,
    exchange_code,
    start_upstream_auth,
)
from patient_access.core.models import OutcomeStatus
from patient_access.core.smart import clear_discovery_cache
from patient_access.core.source_settings import ScopeVersion
from patient_access.dispatch.inline import InlineDispatcher
from patient_access.testing.memory import MemoryAttemptStore, MemoryDirectory, MemoryJobStore

from .helpers import ISS, SELECTION, Clock, launch_participant, participant, source_settings

AUTHORIZE = "https://ehr.example.org/authorize"
TOKEN = "https://ehr.example.org/token"


@pytest.fixture(autouse=True)
def discovery():
    clear_discovery_cache()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.GET,
            f"{ISS}/.well-known/smart-configuration",
            json={
                "authorization_endpoint": AUTHORIZE,
                "token_endpoint": TOKEN,
                "code_challenge_methods_supported": ["S256"],
            },
        )
        yield rsps
    clear_discovery_cache()


def directory():
    return MemoryDirectory([SELECTION], lambda endpoint: source_settings())


def launch(attempts, clock, selection_id="loc1"):
    return start_upstream_auth(
        participant=launch_participant(),
        selection_id=selection_id,
        scopes=("patient/Observation.read",),
        scopes_declined=("patient/Condition.read",),
        redirect_uri="https://jhe.example.org/callback",
        directory=directory(),
        keyring=PUBLIC_ONLY,
        attempts=attempts,
        http=requests.Session(),
        clock=clock,
    )


def test_launch_returns_an_authorize_url_and_records_the_attempt():
    clock, attempts = Clock(), MemoryAttemptStore(Clock())
    url = launch(attempts, clock)
    q = parse_qs(urlparse(url).query)
    assert url.startswith(AUTHORIZE)
    assert q["client_id"] == ["client-1"] and q["aud"] == [ISS] and q["code_challenge_method"] == ["S256"]
    assert q["scope"][0].split() == ["openid", "fhirUser", "launch/patient", "patient/Observation.read"]
    (attempt,) = attempts.attempts.values()
    assert q["state"] == [attempt.state]
    assert attempt.scopes_declined == ("patient/Condition.read",)
    assert attempt.expires_at == clock() + timedelta(minutes=10)


def test_a_v2_registration_spells_requested_and_declined_scopes_alike(monkeypatch):
    """Epic, 2026-11-05: `requested` came out as `.rs` and `declined` as `.read` because only the
    requested list went through the registration's spelling."""
    base = source_settings()
    v2 = base.model_copy(
        update={"registration": base.registration.model_copy(update={"scope_version": ScopeVersion.V2})}
    )
    monkeypatch.setattr(sys.modules[__name__], "source_settings", lambda: v2)
    clock, attempts = Clock(), MemoryAttemptStore(Clock())
    url = launch(attempts, clock)
    assert parse_qs(urlparse(url).query)["scope"][0].split()[-1] == "patient/Observation.rs"
    (attempt,) = attempts.attempts.values()
    assert attempt.scopes_declined == ("patient/Condition.rs",)


def test_an_unknown_selection_or_vendor_is_a_flow_error():
    with pytest.raises(FlowError) as e:
        launch(MemoryAttemptStore(), Clock(), selection_id="nope")
    assert e.value.kind is FlowErrorKind.UNKNOWN_SELECTION

    def unconfigured(endpoint):
        raise KeyError(endpoint.vendor)

    with pytest.raises(FlowError) as e:
        start_upstream_auth(
            participant=launch_participant(),
            selection_id="loc1",
            scopes=(),
            scopes_declined=(),
            redirect_uri="r",
            directory=MemoryDirectory([SELECTION], unconfigured),
            keyring=PUBLIC_ONLY,
            attempts=MemoryAttemptStore(),
            http=requests.Session(),
            clock=Clock(),
        )
    assert e.value.kind is FlowErrorKind.NOT_CONFIGURED


def test_begin_callback_is_single_use_and_expires():
    clock = Clock()
    attempts = MemoryAttemptStore(clock)
    launch(attempts, clock)
    state = next(iter(attempts.attempts))
    assert begin_callback(state, attempts).state == state
    with pytest.raises(FlowError) as e:
        begin_callback(state, attempts)  # replayed
    assert e.value.kind is FlowErrorKind.ATTEMPT_INVALID

    launch(attempts, clock)
    fresh = next(s for s in attempts.attempts if s != state)
    clock.advance(minutes=11)
    with pytest.raises(FlowError):
        begin_callback(fresh, attempts)
    with pytest.raises(FlowError):
        begin_callback("unknown", attempts)


def consumed_attempt(clock):
    attempts = MemoryAttemptStore(clock)
    launch(attempts, clock)
    return begin_callback(next(iter(attempts.attempts)), attempts)


def test_exchange_posts_the_code_with_the_verifier_as_a_public_client(discovery):
    discovery.add(
        responses.POST,
        TOKEN,
        json={
            "access_token": "ehr-token",
            "patient": "p1",
            "scope": "openid patient/Observation.read",
            "expires_in": 600,
        },
    )
    clock = Clock()
    attempt = consumed_attempt(clock)
    token = exchange_code(
        attempt,
        code="code-1",
        error=None,
        directory=directory(),
        keyring=PUBLIC_ONLY,
        http=requests.Session(),
        clock=clock,
    )
    body = parse_qs(discovery.calls[-1].request.body)
    assert body["client_id"] == ["client-1"] and body["code"] == ["code-1"]
    assert body["code_verifier"] == [attempt.code_verifier.get_secret_value()]
    assert token.patient == "p1" and token.access_token.get_secret_value() == "ehr-token"
    assert token.expires_at == clock() + timedelta(seconds=600)
    assert "ehr-token" not in repr(token)


@pytest.mark.parametrize(
    ("code", "error", "kind"),
    [(None, "access_denied", FlowErrorKind.DECLINED), (None, None, FlowErrorKind.NO_CODE)],
)
def test_a_declined_or_codeless_callback_never_reaches_the_ehr(discovery, code, error, kind):
    with pytest.raises(FlowError) as e:
        exchange_code(
            consumed_attempt(Clock()),
            code=code,
            error=error,
            directory=directory(),
            keyring=PUBLIC_ONLY,
            http=requests.Session(),
            clock=Clock(),
        )
    assert e.value.kind is kind
    assert not any(c.request.method == "POST" for c in discovery.calls)


def test_a_refused_exchange_is_a_flow_error_without_the_ehrs_text(discovery):
    discovery.add(responses.POST, TOKEN, status=400, json={"error": "invalid_grant", "error_description": "for MRN 1"})
    with pytest.raises(FlowError) as e:
        exchange_code(
            consumed_attempt(Clock()),
            code="c",
            error=None,
            directory=directory(),
            keyring=PUBLIC_ONLY,
            http=requests.Session(),
            clock=Clock(),
        )
    assert e.value.kind is FlowErrorKind.SIGN_IN_UNAVAILABLE and "MRN" not in str(e.value)


def test_create_job_plans_outcomes_and_dispatches(discovery):
    discovery.add(
        responses.POST, TOKEN, json={"access_token": "t", "patient": "p1", "scope": "openid patient/Observation.read"}
    )
    clock = Clock()
    attempt = consumed_attempt(clock)
    token = exchange_code(
        attempt, code="c", error=None, directory=directory(), keyring=PUBLIC_ONLY, http=requests.Session(), clock=clock
    )
    jobs, dispatched = MemoryJobStore(clock), []
    job = create_job(
        attempt,
        token,
        participant(),
        directory=directory(),
        jobs=jobs,
        dispatcher=InlineDispatcher(dispatched.append),
        clock=clock,
    )
    assert dispatched == [job.id]
    stored = jobs.get(job.id)
    assert stored.credentials.ehr_access_token.get_secret_value() == "t"
    assert stored.source.ehr_patient_id == "p1" and stored.source.facility_name == "Example Clinic"
    statuses = {o.label: o.status for o in stored.outcomes}
    assert statuses["Labs"] is OutcomeStatus.PENDING and statuses["Conditions"] is OutcomeStatus.NOT_REQUESTED
