"""Elevating the OMH effective_time_frame onto queryable, FHIR-shaped Observation columns.

The single point-in-time populates effective_date_time (-> FHIR effectiveDateTime); every OMH
time_interval form populates effective_period_start/end (-> FHIR effectivePeriod). omh_data
remains the source of truth; the columns are a derived, indexed projection kept in sync on save.
"""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from core.fhir.config import get_resource_mapping
from core.fhir.effective_time_frame import extract_effective_time_frame
from core.fhir.engine import build_fhir_resource
from core.models import CodeableConcept, Observation
from core.utils import generate_observation_value_attachment_data


def _omh(time_frame):
    return {"body": {"effective_time_frame": time_frame}}


def _dt(*args):
    return datetime(*args, tzinfo=UTC)


# --- extraction, one case per OMH shape -------------------------------------------------


def test_instant_maps_to_date_time():
    assert extract_effective_time_frame(_omh({"date_time": "2026-07-18T09:30:00Z"})) == (
        _dt(2026, 7, 18, 9, 30),
        None,
        None,
    )


def test_naive_date_time_is_utc():
    # Legacy OMH: a missing timezone means UTC.
    dt, start, end = extract_effective_time_frame(_omh({"date_time": "2026-07-18T09:30:00"}))
    assert (dt, start, end) == (_dt(2026, 7, 18, 9, 30), None, None)


def test_start_and_end_maps_to_period():
    assert extract_effective_time_frame(
        _omh({"time_interval": {"start_date_time": "2026-07-18T09:00:00Z", "end_date_time": "2026-07-18T10:00:00Z"}})
    ) == (None, _dt(2026, 7, 18, 9), _dt(2026, 7, 18, 10))


def test_start_and_duration_computes_end():
    assert extract_effective_time_frame(
        _omh({"time_interval": {"start_date_time": "2026-07-18T09:00:00Z", "duration": {"value": 60, "unit": "min"}}})
    ) == (None, _dt(2026, 7, 18, 9), _dt(2026, 7, 18, 10))


def test_end_and_duration_computes_start():
    assert extract_effective_time_frame(
        _omh({"time_interval": {"end_date_time": "2026-07-18T10:00:00Z", "duration": {"value": 60, "unit": "min"}}})
    ) == (None, _dt(2026, 7, 18, 9), _dt(2026, 7, 18, 10))


def test_duration_in_months_clamps_to_month_end():
    # Jan 31 + 1 month clamps to Feb 28 (2026 is not a leap year).
    assert extract_effective_time_frame(
        _omh({"time_interval": {"start_date_time": "2026-01-31T00:00:00Z", "duration": {"value": 1, "unit": "Mo"}}})
    ) == (None, _dt(2026, 1, 31), _dt(2026, 2, 28))


@pytest.mark.parametrize(
    "part,start_hour,end_hour",
    [("morning", 6, 12), ("afternoon", 12, 18), ("evening", 18, 24), ("night", 0, 6)],
)
def test_date_and_part_of_day_maps_to_window(part, start_hour, end_hour):
    _, start, end = extract_effective_time_frame(_omh({"time_interval": {"date": "2026-07-18", "part_of_day": part}}))
    assert start == _dt(2026, 7, 18, start_hour)
    # ``evening`` runs to the following midnight.
    assert end == (_dt(2026, 7, 19) if end_hour == 24 else _dt(2026, 7, 18, end_hour))


@pytest.mark.parametrize(
    "omh_data",
    [
        {},
        {"body": {}},
        {"body": {"effective_time_frame": {}}},
        {"body": {"effective_time_frame": {"date_time": 123}}},
        "not-a-dict",
        None,
    ],
)
def test_malformed_yields_all_none(omh_data):
    assert extract_effective_time_frame(omh_data or {}) == (None, None, None)


# --- model sync + FHIR rendering --------------------------------------------------------


def _render_effective(time_frame):
    observation = Observation(id=1, omh_data=_omh(time_frame))
    observation._sync_effective_time_frame()
    rendered = build_fhir_resource(observation, "Observation", get_resource_mapping("Observation"))
    return observation, {k: v for k, v in rendered.items() if k.startswith("effective")}


def test_sync_and_render_instant(db):
    observation, effective = _render_effective({"date_time": "2026-07-18T09:30:00Z"})
    assert observation.effective_date_time == _dt(2026, 7, 18, 9, 30)
    assert observation.effective_period_start is None and observation.effective_period_end is None
    assert effective == {"effectiveDateTime": observation.effective_date_time}


def test_sync_and_render_period(db):
    observation, effective = _render_effective(
        {"time_interval": {"start_date_time": "2026-07-18T09:00:00Z", "end_date_time": "2026-07-18T10:00:00Z"}}
    )
    assert observation.effective_date_time is None
    assert effective == {
        "effectivePeriod": {
            "start": observation.effective_period_start,
            "end": observation.effective_period_end,
        }
    }


def test_no_time_frame_renders_no_effective(db):
    observation, effective = _render_effective({})
    assert observation.effective_date_time is None
    assert effective == {}


def test_save_keeps_columns_in_sync(db, patient):
    # A real, schema-valid heart-rate payload so save() -> clean() passes; the generator seeds an
    # instant effective_time_frame (date_time).
    code = "omh:heart-rate:2.0"
    concept = CodeableConcept.objects.create(coding_system="https://w3id.org/openmhealth", coding_code=code, text=code)
    payload = generate_observation_value_attachment_data(code)
    observation = Observation.objects.create(subject_patient=patient, codeable_concept=concept, omh_data=payload)
    observation.refresh_from_db()
    assert observation.effective_date_time is not None
    assert observation.effective_period_start is None and observation.effective_period_end is None

    # A later edit to an interval re-projects the columns from the new omh_data.
    payload = deepcopy(payload)
    payload["body"]["effective_time_frame"] = {
        "time_interval": {"start_date_time": "2026-07-18T09:00:00Z", "end_date_time": "2026-07-18T10:00:00Z"}
    }
    observation.omh_data = payload
    observation.save()
    observation.refresh_from_db()
    assert observation.effective_date_time is None
    assert observation.effective_period_start == _dt(2026, 7, 18, 9)
    assert observation.effective_period_end == _dt(2026, 7, 18, 10)
