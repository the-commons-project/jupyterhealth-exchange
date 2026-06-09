import random
from datetime import UTC, datetime

from core.management.commands import seed_rich_demo as gen


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


from datetime import date


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


import pytest
from django.core.management import call_command
from django.utils import timezone as dj_timezone

from core.models import CodeableConcept, Observation, Patient, Study


@pytest.fixture
def planetary_org(db):
    from core.models import Organization

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

    cgm = CodeableConcept.objects.get(coding_code=gen.CGM_CODE)
    assert Observation.objects.filter(codeable_concept=cgm).count() > 0

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
