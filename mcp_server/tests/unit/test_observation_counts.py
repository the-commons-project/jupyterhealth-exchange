from unittest.mock import AsyncMock

import pytest
from jhe_mcp.auth.context import AuthContext, set_current_auth
from jhe_mcp.fhir.models import StudyPatient
from jhe_mcp.tools.observation_counts import (
    count_patient_observations,
    count_study_observations,
)


@pytest.fixture
def auth():
    token = set_current_auth(AuthContext(bearer_token="t", subject="u", expires_at=0))
    yield
    from jhe_mcp.auth.context import _current

    _current.reset(token)


@pytest.fixture
def fake_client(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr("jhe_mcp.tools.observation_counts.JheClient", lambda _base_url: client)
    return client


@pytest.fixture(autouse=True)
def no_date_preflight(monkeypatch):
    # Default: capabilities unknown -> date preflight is a no-op.
    async def _noop(base_url, start, end):
        return None

    monkeypatch.setattr("jhe_mcp.tools.observation_counts.preflight_observation_dates", _noop)


@pytest.mark.asyncio
async def test_count_patient_observations(auth, fake_client):
    fake_client.fhir_get.return_value = {"total": 57, "entry": []}
    n = await count_patient_observations(patient_id="40006", base_url="http://jhe")
    assert n == 57
    sent = fake_client.fhir_get.await_args.kwargs["params"]
    assert sent["patient"] == "40006" and sent["_summary"] == "count"


@pytest.mark.asyncio
async def test_count_study_observations_total(auth, fake_client):
    fake_client.fhir_get.return_value = {"total": 980, "entry": []}
    n = await count_study_observations(study_id="30006", base_url="http://jhe")
    assert n == 980
    sent = fake_client.fhir_get.await_args.kwargs["params"]
    assert sent["patient._has:_group:member:_id"] == "30006"
    assert sent["_summary"] == "count"


@pytest.mark.asyncio
async def test_count_study_observations_by_patient(auth, fake_client, monkeypatch):
    async def fake_list(*, study_id, base_url):
        return [
            StudyPatient(patient_id="40006", given_name="May", family_name="Nguyen"),
            StudyPatient(patient_id="40007", given_name="Al", family_name="Roe"),
        ]

    monkeypatch.setattr("jhe_mcp.tools.observation_counts.list_study_patients", fake_list)
    fake_client.fhir_get.side_effect = [
        {"total": 600, "entry": []},
        {"total": 380, "entry": []},
    ]
    result = await count_study_observations(study_id="30006", by_patient=True, base_url="http://jhe")
    assert result == {"40006": 600, "40007": 380}


@pytest.mark.asyncio
async def test_count_patient_observations_date_filter_is_server_side(auth, fake_client):
    fake_client.fhir_get.return_value = {"total": 2}
    n = await count_patient_observations(
        patient_id="40006", start="2026-04-01", end="2026-04-30", base_url="http://jhe"
    )
    assert n == 2  # server-filtered total, no record fetch
    sent = fake_client.fhir_get.await_args.kwargs["params"]
    assert sent["_summary"] == "count"
    assert sent["date"] == ["ge2026-04-01", "le2026-04-30"]


@pytest.mark.asyncio
async def test_count_patient_observations_ambiguous_data_type_falls_back_to_omh(auth, fake_client):
    fake_client.fhir_get.side_effect = [{"total": 0}, {"total": 9}, {"total": 9}]
    n = await count_patient_observations(patient_id="40126", data_type="sleep-episode", base_url="http://jhe")
    assert n == 9
    codes = [c.kwargs["params"]["code"] for c in fake_client.fhir_get.await_args_list]
    assert codes == [
        "https://w3id.org/ieee1752|ieee:sleep-episode:1.0",
        "https://w3id.org/openmhealth|omh:sleep-episode:1.1",
        "https://w3id.org/openmhealth|omh:sleep-episode:1.1",
    ]


@pytest.mark.asyncio
async def test_count_study_observations_by_patient_resolves_once_at_study_level(auth, fake_client, monkeypatch):
    async def fake_list(*, study_id, base_url):
        return [
            StudyPatient(patient_id="40006", given_name="May", family_name="Nguyen"),
            StudyPatient(patient_id="40007", given_name="Al", family_name="Roe"),
        ]

    monkeypatch.setattr("jhe_mcp.tools.observation_counts.list_study_patients", fake_list)
    # Study-level probes: IEEE empty, OMH populated; then one count per patient.
    fake_client.fhir_get.side_effect = [{"total": 0}, {"total": 5}, {"total": 3}, {"total": 2}]
    result = await count_study_observations(
        study_id="30008", data_type="sleep-episode", by_patient=True, base_url="http://jhe"
    )
    assert result == {"40006": 3, "40007": 2}
    sent = [c.kwargs["params"] for c in fake_client.fhir_get.await_args_list]
    assert sent[0]["patient._has:_group:member:_id"] == "30008" and sent[1]["patient._has:_group:member:_id"] == "30008"
    for per_patient in sent[2:]:
        assert per_patient["code"] == "https://w3id.org/openmhealth|omh:sleep-episode:1.1"
        assert "patient._has:_group:member:_id" not in per_patient  # per-patient counts stay unscoped, as before
    assert [p["patient"] for p in sent[2:]] == ["40006", "40007"]
