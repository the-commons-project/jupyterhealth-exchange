from unittest.mock import AsyncMock

import pytest
from jhe_mcp.fhir.observation_query import (
    build_observation_params,
    count_observations,
    fetch_observation_page,
    iter_all_observations,
)


def test_build_params_patient_and_filters():
    params = build_observation_params(patient_id="7", data_type="blood-glucose", start="2026-04-01", end="2026-05-01")
    assert params["patient"] == "7"
    assert "omh:blood-glucose:4.0" in params["code"]
    assert params["date"] == ["ge2026-04-01", "le2026-05-01"]


def test_build_params_study_scope():
    params = build_observation_params(study_id="30006")
    assert params["patient._has:_group:member:_id"] == "30006"


def test_build_params_unknown_data_type_raises():
    with pytest.raises(ValueError, match="Unknown data_type"):
        build_observation_params(patient_id="7", data_type="not-a-type")


@pytest.mark.asyncio
async def test_count_observations_reads_total_not_entries():
    client = AsyncMock()
    client.fhir_get.return_value = {"resourceType": "Bundle", "total": 4242, "entry": [{}]}
    n = await count_observations(client, {"patient": "7"})
    assert n == 4242
    sent = client.fhir_get.await_args.kwargs["params"]
    assert sent["_count"] == 1
    assert sent["patient"] == "7"


@pytest.mark.asyncio
async def test_fetch_page_returns_total_entries_has_more():
    client = AsyncMock()
    client.fhir_get.return_value = {"total": 130, "entry": [{"resource": {"id": "a"}}]}
    total, entries, has_more = await fetch_observation_page(client, {"patient": "7"}, page=1, page_size=50)
    assert total == 130
    assert len(entries) == 1
    assert has_more is True
    sent = client.fhir_get.await_args.kwargs["params"]
    assert sent["_count"] == 50 and sent["_page"] == 1


@pytest.mark.asyncio
async def test_fetch_page_last_page_has_no_more():
    client = AsyncMock()
    client.fhir_get.return_value = {"total": 40, "entry": [{} for _ in range(40)]}
    total, entries, has_more = await fetch_observation_page(client, {"patient": "7"}, page=1, page_size=50)
    assert total == 40 and has_more is False


@pytest.mark.asyncio
async def test_iter_all_follows_pages():
    client = AsyncMock()
    client.fhir_get.side_effect = [
        {"total": 1500, "entry": [{"resource": {"id": str(i)}} for i in range(1000)]},
        {"total": 1500, "entry": [{"resource": {"id": str(i)}} for i in range(500)]},
    ]
    entries = await iter_all_observations(client, {"patient": "7"})
    assert len(entries) == 1500
    assert client.fhir_get.await_count == 2
