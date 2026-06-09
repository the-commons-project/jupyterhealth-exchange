import random
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone as dj_timezone

from core.management.commands import seed_rich_demo as gen
from core.management.commands.seed import Command as SeedCommand
from core.models import CodeableConcept, Observation, Organization, Patient, Study


def test_cgm_value_stays_in_physiologic_range():
    rng = random.Random("test")
    for hour in range(24):
        dt = datetime(2026, 6, 1, hour, 0, tzinfo=UTC)
        for risk in (0.0, 0.4, 0.85):
            v = gen.cgm_value(dt, risk, rng)
            assert 40 <= v <= 300
            assert isinstance(v, int)


def test_cgm_value_rises_after_a_meal():
    pre_meal = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)  # before breakfast
    post_meal = datetime(2026, 6, 1, 8, 15, tzinfo=UTC)  # ~45 min after 7:30
    # Average several draws to wash out gaussian noise.
    pre = sum(gen.cgm_value(pre_meal, 0.4, random.Random(i)) for i in range(50)) / 50
    post = sum(gen.cgm_value(post_meal, 0.4, random.Random(i)) for i in range(50)) / 50
    assert post > pre + 15


def test_risk_score_increases_with_age():
    assert gen.risk_score(30) <= gen.risk_score(60) <= gen.risk_score(90)
    assert 0.0 <= gen.risk_score(95) <= 0.85


def test_generate_wearable_day_returns_all_eight_typed_records():
    rng = random.Random("p1")
    records = gen.generate_wearable_day(date(2026, 6, 1), 0, age=55, risk=0.3, rng=rng)
    expected_codes = {code for code, _system, _label in gen.WEARABLE_SCOPES}
    assert set(records.keys()) == expected_codes
    for code, rec in records.items():
        assert "header" in rec and "body" in rec
        assert rec["header"]["acquisition_provenance"]["source_name"] == gen.WEARABLE_SOURCE_NAME


@pytest.mark.django_db
def test_generated_wearable_bodies_are_schema_valid():
    """Each generated wearable body must pass Observation.clean() — the OMH schema oracle."""
    rng = random.Random("v")
    records = gen.generate_wearable_day(date(2026, 6, 1), 0, age=55, risk=0.3, rng=rng)
    for code, system, label in gen.WEARABLE_SCOPES:
        cc = CodeableConcept.objects.create(coding_code=code, coding_system=system, text=label)
        obs = Observation(codeable_concept=cc, omh_data=records[code])
        # clean() raises ValidationError / RuntimeError if header or body is invalid
        obs.clean()  # must not raise


@pytest.mark.django_db
def test_cgm_body_is_schema_valid():
    """CGM body must pass Observation.clean() — locks in blood-glucose:4.0 conformance."""
    dt = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    cc = CodeableConcept.objects.create(
        coding_code=gen.CGM_CODE,
        coding_system=gen.OMH,
        text="Blood glucose",
    )
    obs = Observation(codeable_concept=cc, omh_data=gen.cgm_body(dt, 120))
    obs.clean()  # must not raise


@pytest.fixture
def planetary_org(db):
    return Organization.objects.create(name="Planetary Research Institute", type="edu")


@pytest.mark.django_db
def test_seed_rich_demo_builds_full_cohort(planetary_org, monkeypatch):
    # Shrink the generated window so the test stays fast (still exercises all
    # record types + anchoring). The command reads these as module globals.
    monkeypatch.setattr(gen, "CGM_WINDOW_DAYS", 1)
    monkeypatch.setattr(gen, "WEARABLE_MIN_DAYS", 2)
    monkeypatch.setattr(gen, "WEARABLE_MAX_DAYS", 2)

    call_command("seed_rich_demo")

    study = Study.objects.get(name=gen.STUDY_NAME)
    patients = Patient.objects.filter(studypatient__study=study).distinct()
    assert patients.count() == len(gen.MOCK_PATIENTS)

    # Every patient contributes a full CGM window, not just "some" data.
    cgm = CodeableConcept.objects.get(coding_code=gen.CGM_CODE)
    expected_cgm = ((gen.CGM_WINDOW_DAYS * 24 * 60) // gen.CGM_INTERVAL_MINUTES + 1) * len(gen.MOCK_PATIENTS)
    assert Observation.objects.filter(codeable_concept=cgm).count() == expected_cgm

    # All 8 wearable types present.
    for code, _system, _label in gen.WEARABLE_SCOPES:
        cc = CodeableConcept.objects.get(coding_code=code)
        assert Observation.objects.filter(codeable_concept=cc).exists(), code

    # Data is anchored to today: the latest CGM reading is on the run date (UTC).
    latest = (
        Observation.objects.filter(codeable_concept=cgm)
        .order_by("-omh_data__header__source_creation_date_time")
        .first()
    )
    latest_date = latest.omh_data["header"]["source_creation_date_time"][:10]
    assert latest_date == dj_timezone.now().date().isoformat()


@pytest.mark.django_db
def test_with_rich_demo_flag_invokes_generator():
    with patch("core.management.commands.seed.call_command") as mock_call:
        SeedCommand().handle(flush_db=False, with_rich_demo=True)
    assert any(c.args and c.args[0] == "seed_rich_demo" for c in mock_call.call_args_list)


@pytest.mark.django_db
def test_without_flag_does_not_invoke_generator():
    with patch("core.management.commands.seed.call_command") as mock_call:
        SeedCommand().handle(flush_db=False, with_rich_demo=False)
    assert not any(c.args and c.args[0] == "seed_rich_demo" for c in mock_call.call_args_list)
