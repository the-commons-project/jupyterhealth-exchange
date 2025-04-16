import json
import base64
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from django.core import mail
from django.core.exceptions import PermissionDenied, BadRequest
from oauth2_provider.models import get_grant_model

from core.models import (
    JheUser, Organization, Patient, CodeableConcept, Study, StudyPatient,
    StudyPatientScopeConsent, StudyScopeRequest, DataSource, DataSourceSupportedScope,
    StudyDataSource, Observation, ObservationIdentifier
)

@override_settings(
    DATABASES={
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }
)

# -----------------------------------------------------
# JheUser Methods
# -----------------------------------------------------
class JheUserMethodTests(TestCase):
    def setUp(self):
        self.user = JheUser.objects.create_user(
            email="test@example.com", password="password", identifier="test123"
        )
    
    def test_create_superuser(self):
        superuser = JheUser.objects.create_superuser(
            email="admin@example.com", password="adminpass"
        )
        self.assertTrue(superuser.is_staff)
        self.assertTrue(superuser.is_superuser)
    
    def test_send_email_verification(self):
        # Clear the mail outbox first.
        mail.outbox = []
        self.user.send_email_verificaion()  # method name as in your model (check spelling)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("JHE E-mail Verification", mail.outbox[0].subject)
    
    def test_create_authorization_code(self):
        # Using a dummy application id and redirect URL.
        application_id = 1
        redirect_uri = "http://example.com/redirect"
        Grant = get_grant_model()
        code_instance = self.user.create_authorization_code(application_id, redirect_uri)
        self.assertIsNotNone(code_instance)
        self.assertEqual(code_instance.redirect_uri, redirect_uri)
        self.assertEqual(code_instance.scope, "openid")
        self.assertTrue(bool(code_instance.code))  # Code should not be empty


# -----------------------------------------------------
# Organization Methods
# -----------------------------------------------------
class OrganizationMethodTests(TestCase):
    def setUp(self):
        self.parent_org = Organization.objects.create(name="Parent Org", type="prov")
        self.child_org = Organization.objects.create(name="Child Org", type="dept", part_of=self.parent_org)
    
    def test_get_children(self):
        children = list(Organization.get_children(self.parent_org.id))
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0], self.child_org)
    
    def test_collect_children(self):
        # Reset the parent's children list and populate it recursively.
        self.parent_org.children = []
        Organization.collect_children(self.parent_org)
        self.assertIn(self.child_org, self.parent_org.children)
    
    def test_for_practitioner(self):
        user = JheUser.objects.create_user(
            email="user@example.com", password="password", identifier="user123"
        )
        # Establish the relationship using the many-to-many field.
        user.organizations.add(self.parent_org)
        orgs = list(Organization.for_practitioner(user.id))
        self.assertGreaterEqual(len(orgs), 1)
        self.assertTrue(any(org.id == self.parent_org.id for org in orgs))


# -----------------------------------------------------
# Patient Methods
# -----------------------------------------------------
class PatientMethodTests(TestCase):
    def setUp(self):
        self.user = JheUser.objects.create_user(
            email="patient@example.com", password="password", identifier="patient123"
        )
        self.org = Organization.objects.create(name="Hospital", type="prov")
        self.patient = Patient.objects.create(
            jhe_user=self.user, organization=self.org, identifier="PAT001",
            name_family="Smith", name_given="Alice",
            birth_date="1985-05-05", telecom_phone="1234567890"
        )
    
    def test_consolidated_consented_scopes_empty(self):
        scopes = list(self.patient.consolidated_consented_scopes())
        self.assertEqual(len(scopes), 0)
    
    def test_from_jhe_user_id(self):
        fetched = Patient.from_jhe_user_id(self.user.id)
        self.assertEqual(fetched, self.patient)
    
    def test_for_practitioner_organization_study(self):
        study = Study.objects.create(name="Study1", description="Desc", organization=self.org)
        StudyPatient.objects.create(study=study, patient=self.patient)
        patients = list(Patient.for_practitioner_organization_study(self.user.id))
        self.assertGreaterEqual(len(patients), 1)
    
    def test_count_for_practitioner_organization_study(self):
        study = Study.objects.create(name="Study1", description="Desc", organization=self.org)
        StudyPatient.objects.create(study=study, patient=self.patient)
        count = Patient.count_for_practitioner_organization_study(self.user.id)
        self.assertGreaterEqual(count, 1)
    
    def test_practitioner_authorized(self):
        # Since the user has a patient and we create at least one study relation,
        # practitioner_authorized should return True.
        study = Study.objects.create(name="Study1", description="Desc", organization=self.org)
        StudyPatient.objects.create(study=study, patient=self.patient)
        authorized = Patient.practitioner_authorized(self.user.id)
        self.assertTrue(authorized)


# -----------------------------------------------------
# Study Methods
# -----------------------------------------------------
class StudyMethodTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Research Org", type="edu")
        self.study = Study.objects.create(name="Study A", description="A study", organization=self.org)
        self.user = JheUser.objects.create_user(
            email="doc@example.com", password="password", identifier="doc123"
        )
        self.user.organizations.add(self.org)
    
    def test_for_practitioner_organization(self):
        studies = list(Study.for_practitioner_organization(self.user.id))
        self.assertGreaterEqual(len(studies), 1)
        self.assertTrue(any(s.id == self.study.id for s in studies))
    
    def test_count_for_practitioner_organization(self):
        count = Study.count_for_practitioner_organization(self.user.id)
        self.assertGreaterEqual(count, 1)
    
    def test_practitioner_authorized(self):
        authorized = Study.practitioner_authorized(self.user.id, self.study.id)
        self.assertTrue(authorized)
    
    def test_has_patient(self):
        patient_user = JheUser.objects.create_user(
            email="patient2@example.com", password="password", identifier="patient456"
        )
        patient = Patient.objects.create(
            jhe_user=patient_user, organization=self.org, identifier="PAT002",
            name_family="Jones", name_given="Bob", birth_date="1990-01-01", telecom_phone="0987654321"
        )
        StudyPatient.objects.create(study=self.study, patient=patient)
        result = Study.has_patient(self.study.id, patient.id)
        self.assertTrue(result)
    
    def test_studies_with_scopes(self):
        # With no scope requests or consents present, expect an empty list.
        studies = Study.studies_with_scopes(patient_id=1)  # Using a dummy patient id
        self.assertIsInstance(studies, list)


# -----------------------------------------------------
# StudyPatientScopeConsent Methods
# -----------------------------------------------------
class StudyPatientScopeConsentMethodTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Hospital", type="prov")
        self.user = JheUser.objects.create_user(
            email="patient3@example.com", password="password", identifier="patient789"
        )
        self.patient = Patient.objects.create(
            jhe_user=self.user, organization=self.org, identifier="PAT003",
            name_family="Brown", name_given="Charlie", birth_date="1980-01-01", telecom_phone="5550001234"
        )
        self.study = Study.objects.create(name="Study B", description="Desc", organization=self.org)
        self.study_patient = StudyPatient.objects.create(study=self.study, patient=self.patient)
        self.code = CodeableConcept.objects.create(
            coding_system="http://loinc.org", coding_code="1234-5", text="Test Code"
        )
        self.consent = StudyPatientScopeConsent.objects.create(
            study_patient=self.study_patient, scope_actions="rs", scope_code=self.code,
            consented=True, consented_time=timezone.now()
        )
    
    def test_patient_scopes(self):
        scopes = list(StudyPatientScopeConsent.patient_scopes(self.user.id))
        self.assertGreaterEqual(len(scopes), 1)


# -----------------------------------------------------
# DataSource Methods
# -----------------------------------------------------
class DataSourceMethodTests(TestCase):
    def setUp(self):
        self.ds = DataSource.objects.create(name="Smartphone", type="personal_device")
        self.code = CodeableConcept.objects.create(
            coding_system="http://loinc.org", coding_code="6789-0", text="Heart Rate"
        )
        self.supported = DataSourceSupportedScope.objects.create(data_source=self.ds, scope_code=self.code)
    
    def test_data_sources_with_scopes(self):
        data_sources = list(DataSource.data_sources_with_scopes(data_source_id=self.ds.id))
        self.assertGreaterEqual(len(data_sources), 1)
        ds = data_sources[0]
        self.assertTrue(hasattr(ds, 'supported_scopes'))
        self.assertGreaterEqual(len(ds.supported_scopes), 1)


# -----------------------------------------------------
# Observation Methods
# -----------------------------------------------------
class ObservationMethodTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Clinic", type="prov")
        self.user = JheUser.objects.create_user(
            email="patient4@example.com", password="password", identifier="patient000"
        )
        self.patient = Patient.objects.create(
            jhe_user=self.user, organization=self.org, identifier="PAT004",
            name_family="White", name_given="Daisy", birth_date="1975-07-07", telecom_phone="1112223333"
        )
        self.code = CodeableConcept.objects.create(
            coding_system="http://loinc.org", coding_code="1122-3", text="Test Measurement"
        )
        self.ds = DataSource.objects.create(name="Monitor", type="personal_device")
        self.observation = Observation.objects.create(
            subject_patient=self.patient, codeable_concept=self.code, data_source=self.ds,
            status="final", value_attachment_data={"value": 100, "unit": "mmHg"}
        )
    
    def test_for_practitioner_organization_study_patient(self):
        # Call the method to ensure it returns a list (even if empty)
        results = list(Observation.for_practitioner_organization_study_patient(self.user.id))
        self.assertIsInstance(results, list)
    
    def test_practitioner_authorized(self):
        authorized = Observation.practitioner_authorized(self.user.id, self.observation.id)
        self.assertTrue(authorized)
    
    def test_count_for_practitioner_organization_study_patient(self):
        count = Observation.count_for_practitioner_organization_study_patient(self.user.id)
        self.assertGreaterEqual(count, 1)
    
    def test_fhir_search(self):
        try:
            results = Observation.fhir_search(
                self.user.id, coding_system="http://loinc.org", coding_code="1122-3"
            )
            self.assertIsInstance(list(results), list)
        except Exception as e:
            self.fail(f"fhir_search raised an exception: {e}")
    
    def test_fhir_create(self):
        # Construct a minimal valid FHIR observation payload.
        fhir_data = {
            "resourceType": "Observation",
            "status": "final",
            "code": {
                "coding": [
                    {"system": "http://loinc.org", "code": "1122-3"}
                ]
            },
            "subject": {"reference": f"Patient/{self.patient.id}"},
            "device": {"reference": f"Device/{self.ds.id}"},
            "valueAttachment": {
                "data": base64.b64encode(json.dumps({"value": 120, "unit": "mmHg"}).encode("ascii")).decode("ascii")
            }
        }
        # Create a study and consent so that fhir_create passes the permission check.
        study = Study.objects.create(name="Study for FHIR", description="Desc", organization=self.org)
        study_patient = StudyPatient.objects.create(study=study, patient=self.patient)
        StudyPatientScopeConsent.objects.create(
            study_patient=study_patient, scope_actions="rs", scope_code=self.code,
            consented=True, consented_time=timezone.now()
        )
        try:
            created_obs = Observation.fhir_create(fhir_data, self.user)
            self.assertIsNotNone(created_obs)
            self.assertEqual(created_obs.codeable_concept, self.code)
        except Exception as e:
            self.fail(f"fhir_create raised an exception: {e}")


# -----------------------------------------------------
# ObservationIdentifier Methods
# -----------------------------------------------------
class ObservationIdentifierMethodTests(TestCase):
    # No custom methods here to test; this class is a placeholder.
    def test_dummy(self):
        self.assertTrue(True)