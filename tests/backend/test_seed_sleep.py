"""The seed's sleep wiring: Oura + the Open Wearables client on the Sleep & BP study.

Nothing else in the suite runs the full `seed` command, and a data point that fails schema
validation aborts it (Observation.save -> clean), so this doubles as a smoke test that the
seed still completes.
"""

import pytest
from django.core.management import call_command
from oauth2_provider.models import get_application_model

from core.models import (
    ClientDataSource,
    DataSource,
    DataSourceSupportedScope,
    Observation,
    Patient,
    Study,
    StudyClient,
    StudyDataSource,
    StudyScopeRequest,
)

SLEEP_CODE = "ieee:sleep-episode:1.0"
STUDY_NAME = "Lifespan Study on Sleep & BP"


@pytest.fixture
def seeded(db):
    call_command("seed")


def test_oura_supports_sleep_episode_and_heart_rate(seeded):
    oura = DataSource.objects.get(name="Oura")
    scopes = set(
        DataSourceSupportedScope.objects.filter(data_source=oura).values_list("scope_code__coding_code", flat=True)
    )
    assert scopes == {"omh:heart-rate:2.0", SLEEP_CODE}


def test_sleep_study_requests_sleep_from_oura_via_ow_client(seeded):
    study = Study.objects.get(name=STUDY_NAME)
    assert SLEEP_CODE in set(
        StudyScopeRequest.objects.filter(study=study).values_list("scope_code__coding_code", flat=True)
    )
    assert "Oura" in set(StudyDataSource.objects.filter(study=study).values_list("data_source__name", flat=True))
    assert "Open Wearables" in set(StudyClient.objects.filter(study=study).values_list("client__name", flat=True))

    ow = get_application_model().objects.get(name="Open Wearables")
    assert list(ClientDataSource.objects.filter(client=ow).values_list("data_source__name", flat=True)) == ["Oura"]


@pytest.mark.parametrize("email", ["ll_patient_peter@example.com", "ll_patient_pamela@example.com"])
def test_patients_have_schema_valid_sleep_observations(seeded, email):
    patient = Patient.objects.get(jhe_user__email=email)
    observations = Observation.objects.filter(subject_patient=patient, codeable_concept__coding_code=SLEEP_CODE)
    assert observations.count() == 2
    for observation in observations:
        observation.clean()  # raises if the body fails the IEEE sleep-episode schema
        assert observation.data_source.name == "Oura"
        # sleep-episode requires a time_interval, so both period columns must be populated.
        assert observation.effective_period_start and observation.effective_period_end
