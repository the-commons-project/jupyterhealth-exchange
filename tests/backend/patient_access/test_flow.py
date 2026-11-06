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
            "token_type": "Bearer",
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
        responses.POST,
        TOKEN,
        json={"access_token": "t", "token_type": "bearer", "patient": "p1", "scope": "openid patient/Observation.read"},
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


# --- the token response, strictly ------------------------------------------------------------


def exchanged(discovery, **body):
    discovery.add(responses.POST, TOKEN, json={"access_token": "t", "token_type": "Bearer", "patient": "p1"} | body)
    clock = Clock()
    return exchange_code(
        consumed_attempt(clock),
        code="c",
        error=None,
        directory=directory(),
        keyring=PUBLIC_ONLY,
        http=requests.Session(),
        clock=clock,
    ), clock


@pytest.mark.parametrize(
    ("expires_in", "seconds"),
    [(600, 600), (299.5, 299.5), ("3600.0", 3600), ("600", 600), (None, 3600), ("soon", 3600), ("nan", 3600), (-5, 0)],
)
def test_expires_in_is_read_as_a_number(discovery, expires_in, seconds):
    token, clock = exchanged(discovery, expires_in=expires_in)
    assert token.expires_at == clock() + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "body",
    [
        {"token_type": "mac"},  # not a bearer token
        {"token_type": None},  # missing: RFC 6749 requires it
        {"patient": 12345},  # not a string id
        {"patient": ""},
    ],
)
def test_a_token_response_we_cannot_use_is_a_sign_in_failure(discovery, body):
    with pytest.raises(FlowError) as e:
        exchanged(discovery, **body)
    assert e.value.kind is FlowErrorKind.SIGN_IN_UNAVAILABLE


def test_the_bearer_token_type_is_matched_case_insensitively(discovery):
    token, _ = exchanged(discovery, token_type="bearer")
    assert token.patient == "p1"


# --- the authorize URL and discovery ---------------------------------------------------------


def test_the_pkce_challenge_is_s256_of_the_stored_verifier():
    import base64
    import hashlib

    clock, attempts = Clock(), MemoryAttemptStore(Clock())
    q = parse_qs(urlparse(launch(attempts, clock)).query)
    (attempt,) = attempts.attempts.values()
    digest = hashlib.sha256(attempt.code_verifier.get_secret_value().encode()).digest()
    assert q["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]


def test_an_authorize_endpoint_with_its_own_query_keeps_it(discovery):
    discovery.replace(
        responses.GET,
        f"{ISS}/.well-known/smart-configuration",
        json={"authorization_endpoint": f"{AUTHORIZE}?tenant=t1", "token_endpoint": TOKEN},
    )
    url = launch(MemoryAttemptStore(Clock()), Clock())
    q = parse_qs(urlparse(url).query)
    assert url.count("?") == 1 and q["tenant"] == ["t1"] and q["client_id"] == ["client-1"]


@pytest.mark.parametrize(
    "endpoint", ["http://ehr.example.org/authorize", "//evil.example/authorize", "javascript:alert(1)", "/authorize"]
)
def test_the_authorize_endpoint_must_be_absolute_https(discovery, endpoint):
    discovery.replace(
        responses.GET,
        f"{ISS}/.well-known/smart-configuration",
        json={"authorization_endpoint": endpoint, "token_endpoint": TOKEN},
    )
    with pytest.raises(FlowError) as e:
        launch(MemoryAttemptStore(Clock()), Clock())
    assert e.value.kind is FlowErrorKind.SIGN_IN_UNAVAILABLE


def test_an_authorization_server_on_another_host_is_trusted():
    """SMART allows the authorization server on a different host from the FHIR server."""
    from patient_access.core.smart import SmartConfiguration

    config = SmartConfiguration.parse(
        ISS, {"authorization_endpoint": "https://login.example.com/oauth2/authorize", "token_endpoint": TOKEN}
    )
    assert config.authorization_endpoint.startswith("https://login.example.com/")


def test_http_endpoints_are_allowed_only_when_the_host_says_so():
    from patient_access.core.smart import SmartConfiguration

    raw = {"authorization_endpoint": "http://localhost:9000/authorize", "token_endpoint": "http://localhost:9000/token"}
    with pytest.raises(ValueError):
        SmartConfiguration.parse(ISS, raw)
    assert SmartConfiguration.parse(ISS, raw, allow_http=True).authorization_endpoint.startswith("http://")


def test_discovery_is_cached_per_server_for_a_day(monkeypatch):
    from patient_access.core import smart

    now = [1000.0]
    monkeypatch.setattr(smart.time, "monotonic", lambda: now[0])
    clear_discovery_cache()
    config = {"authorization_endpoint": AUTHORIZE, "token_endpoint": TOKEN}
    other = "https://other.example.org/fhir"
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{ISS}/.well-known/smart-configuration", json=config)
        rsps.add(responses.GET, f"{other}/.well-known/smart-configuration", json=config)
        session = requests.Session()
        smart.discover(ISS, session)
        smart.discover(ISS, session)  # a hit
        smart.discover(other, session)  # keyed per server
        assert len(rsps.calls) == 2
        now[0] += smart.DISCOVERY_TTL_SECONDS + 1
        smart.discover(ISS, session)  # expired: fetched again
        assert len(rsps.calls) == 3
    clear_discovery_cache()


# --- the credential plugs (wired: public only; kept for confidential clients) ----------------


class Keys:
    def kids(self):
        return frozenset({"k1"})

    def public_jwks(self):
        return {"keys": []}

    def sign(self, kid, claims, alg):
        return f"signed:{kid}:{alg}:{claims['aud']}"


class Secrets:
    def has(self, name):
        return name == "s1"

    def get(self, name):
        return "the-secret"


def test_an_auth_method_says_what_it_needs():
    from patient_access.core.credentials import AuthMethod, AuthMethodName

    with pytest.raises(ValueError, match="kid"):
        AuthMethod(method=AuthMethodName.PRIVATE_KEY_JWT)
    with pytest.raises(ValueError, match="secret"):
        AuthMethod(method=AuthMethodName.CLIENT_SECRET_POST)


def test_the_keyring_chooses_the_first_method_it_can_satisfy():
    from patient_access.core.credentials import PUBLIC, AuthMethod, AuthMethodName, CredentialError, Keyring

    jwt = AuthMethod(method=AuthMethodName.PRIVATE_KEY_JWT, kid="k1")
    unknown_kid = AuthMethod(method=AuthMethodName.PRIVATE_KEY_JWT, kid="nope")
    keyring = Keyring(Keys(), Secrets())
    assert keyring.choose((unknown_kid, jwt, PUBLIC)) == jwt
    assert PUBLIC_ONLY.choose((jwt, PUBLIC)) == PUBLIC
    with pytest.raises(CredentialError):
        PUBLIC_ONLY.choose((jwt,))


@pytest.mark.parametrize("method", ["public", "client_secret_basic", "client_secret_post", "private_key_jwt"])
def test_the_token_request_authenticates_the_way_the_launch_chose(method):
    from patient_access.core.credentials import AuthMethod, AuthMethodName, Keyring
    from patient_access.core.smart import _client_auth

    chosen = AuthMethod(
        method=AuthMethodName(method),
        kid="k1" if method == "private_key_jwt" else None,
        secret="s1" if method.startswith("client_secret") else None,
    )
    registration = source_settings().registration.model_copy(update={"methods": (chosen,)})
    attempt = consumed_attempt(Clock()).model_copy(update={"auth_method": chosen.method, "key_id": chosen.kid})
    fields, basic = _client_auth(attempt, registration, Keyring(Keys(), Secrets()))
    expected = {
        "public": ({"client_id": "client-1"}, None),
        "client_secret_basic": ({}, ("client-1", "the-secret")),
        "client_secret_post": ({"client_id": "client-1", "client_secret": "the-secret"}, None),
    }
    if method in expected:
        assert (fields, basic) == expected[method]
    else:
        assert fields["client_assertion"] == f"signed:k1:RS384:{TOKEN}" and basic is None
