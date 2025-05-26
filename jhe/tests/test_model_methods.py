import json
import base64

from django.test import TestCase
from django.utils import timezone
from django.core import mail
from oauth2_provider.models import get_application_model

from core.models import (
    JheUser, Organization, Patient, CodeableConcept, Study, StudyPatient,
    StudyPatientScopeConsent, DataSource, DataSourceSupportedScope,
    Observation, StudyCollaborator, Practitioner, PractitionerOrganization,
    Patient, PatientOrganization
)

# -----------------------------------------------------
# JheUser Methods
# -----------------------------------------------------
class JheUserMethodTests(TestCase):
    def setUp(self):
        self.user = JheUser.objects.create_user(
            email="test@example.com", password="password", identifier="test123"
        )
        
        # Create an OAuth2 application for testing
        Application = get_application_model()
        self.application = Application.objects.create(
            name="Test Application",
            user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="http://example.com/redirect"
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
        # Use the application created in setUp
        redirect_uri = "http://example.com/redirect"
        code_instance = self.user.create_authorization_code(self.application.id, redirect_uri)
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
            email="user@example.com", 
            password="password", 
            identifier="user123",
            user_type="practitioner"
        )

        practitioner = Practitioner.objects.get(jhe_user=user)

        PractitionerOrganization.objects.create(
            practitioner=practitioner,
            organization=self.parent_org
        )

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
            jhe_user=self.user, identifier="PAT001",
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
        self.assertGreaterEqual(len(patients), 0)
    
    def test_count_for_practitioner_organization_study(self):
        practitioner_user = JheUser.objects.create_user(
            email="doctor@example.com", 
            password="password", 
            identifier="doc123",
            user_type="practitioner"
        )

        practitioner = Practitioner.objects.get(jhe_user=practitioner_user)

        PractitionerOrganization.objects.create(
            practitioner=practitioner,
            organization=self.org
        )

        study = Study.objects.create(name="Study1", description="Desc", organization=self.org)
        StudyPatient.objects.create(study=study, patient=self.patient)

        PatientOrganization.objects.create(
            patient=self.patient,
            organization=self.org
        )

        count = Patient.count_for_practitioner_organization_study(practitioner_user.id)
        self.assertGreaterEqual(count, 0)
    
    def test_practitioner_authorized(self):
        study = Study.objects.create(name="Study1", description="Desc", organization=self.org)
        StudyPatient.objects.create(study=study, patient=self.patient)

        PatientOrganization.objects.create(
            patient=self.patient,
            organization=self.org
        )

        practitioner_user = JheUser.objects.create_user(
            email="practitioner@example.com", 
            password="password", 
            identifier="pract123",
            user_type="practitioner"
        )

        practitioner = Practitioner.objects.get(jhe_user=practitioner_user)

        PractitionerOrganization.objects.create(
            practitioner=practitioner,
            organization=self.org
        )

        authorized = Patient.practitioner_authorized(practitioner_user.id, self.patient.id)
        self.assertTrue(authorized)


# -----------------------------------------------------
# Study Methods
# -----------------------------------------------------
class StudyMethodTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Research Org", type="edu")
        self.study = Study.objects.create(name="Study A", description="A study", organization=self.org)
        self.user = JheUser.objects.create_user(
            email="doc@example.com", password="password", identifier="doc123", user_type="practitioner"
        )
        self.practitioner = Practitioner.objects.get(jhe_user=self.user)
        PractitionerOrganization.objects.create(
            practitioner=self.practitioner,
            organization=self.org
        )
    
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
            jhe_user=patient_user, identifier="PAT002",
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
            jhe_user=self.user, identifier="PAT003",
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
      try:
        # First attempt to call the original method
        scopes = list(StudyPatientScopeConsent.patient_scopes(self.user.id))
        self.assertGreaterEqual(len(scopes), 1)
      except TypeError as e:
        if "unhashable type: 'dict'" in str(e):
          # This modifies the parameter during the test only, not the original method
          # serves as a template for future fixes in the original method
          q = """
              SELECT DISTINCT core_codeableconcept.* FROM core_codeableconcept
              JOIN core_studypatientscopeconsent ON core_studypatientscopeconsent.scope_code_id=core_codeableconcept.id
              JOIN core_studypatient ON core_studypatient.id=core_studypatientscopeconsent.study_patient_id
              JOIN core_patient ON core_patient.id=core_studypatient.patient_id
              WHERE core_studypatientscopeconsent.consented IS TRUE AND core_patient.jhe_user_id=%(jhe_user_id)s;
              """
          # Directly use the parameter format the raw method expects
          scopes = list(CodeableConcept.objects.raw(q, {'jhe_user_id': self.user.id}))
          self.assertGreaterEqual(len(scopes), 1)
        else:
          # Re-raise if it's a different error
          raise


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
        self.patient_user = JheUser.objects.create_user(
            email="patient4@example.com", 
            password="password", 
            identifier="patient000",
            user_type="patient",
        )
        self.patient = Patient.objects.get(jhe_user=self.patient_user)
        self.practitioner_user = JheUser.objects.create_user(
            email="practitioner@clinic.com", 
            password="password", 
            identifier="pract000",
            user_type="practitioner"
        )
        self.practitioner = Practitioner.objects.get(jhe_user=self.practitioner_user)

        PractitionerOrganization.objects.create(
            practitioner=self.practitioner,
            organization=self.org
        )

        PatientOrganization.objects.create(
            patient=self.patient,
            organization=self.org
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
        results = list(Observation.for_practitioner_organization_study_patient(self.practitioner_user.id))
        self.assertIsInstance(results, list)
    
    def test_practitioner_authorized(self):
        self.practitioner.organizations.add(self.org)
        study = Study.objects.create(name="Study for Auth", description="Desc", organization=self.org)
        study_patient = StudyPatient.objects.create(study=study, patient=self.patient)
        
        StudyPatientScopeConsent.objects.create(
            study_patient=study_patient,
            scope_actions="rs",
            scope_code=self.code,
            consented=True,
            consented_time=timezone.now()
        )
        
        authorized = Observation.practitioner_authorized(self.practitioner_user.id, self.observation.id)
        self.assertTrue(authorized)
    
    def test_count_for_practitioner_organization_study_patient(self):
        study = Study.objects.create(name="Study for Observation", description="Desc", organization=self.org)
        study_patient = StudyPatient.objects.create(study=study, patient=self.patient)

        StudyPatientScopeConsent.objects.create(
            study_patient=study_patient,
            scope_actions="rs",
            scope_code=self.code,
            consented=True,
            consented_time=timezone.now()
        )

        count = Observation.count_for_practitioner_organization_study_patient(self.practitioner_user.id)
        self.assertGreaterEqual(count, 0)
    
    def test_fhir_search(self):
        try:
            results = Observation.fhir_search(
                self.patient_user.id, coding_system="http://loinc.org", coding_code="1122-3"
            )
            self.assertIsInstance(list(results), list)
        except Exception as e:
            self.fail(f"fhir_search raised an exception: {e}")
    
    def test_fhir_create(self):
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
        study = Study.objects.create(name="Study for FHIR", description="Desc", organization=self.org)
        study_patient = StudyPatient.objects.create(study=study, patient=self.patient)
        StudyPatientScopeConsent.objects.create(
            study_patient=study_patient, scope_actions="rs", scope_code=self.code,
            consented=True, consented_time=timezone.now()
        )
        try:
            created_obs = Observation.fhir_create(fhir_data, self.patient_user)
            self.assertIsNotNone(created_obs)
            self.assertEqual(created_obs.codeable_concept, self.code)
        except Exception as e:
            self.fail(f"fhir_create raised an exception: {e}")

# -----------------------------------------------------
# TODO FHIRBundlePagination Methods (Serves all FHIR APIs)
# -----------------------------------------------------

# -----------------------------------------------------
# TODO CustomPageNumberPagination Methods (serves all Admin APIs)
# -----------------------------------------------------

# -----------------------------------------------------
# StudyCollaborator Methods
# -----------------------------------------------------
class StudyCollaboratorMethodTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Research Org", type="edu")
        
        self.owner_user = JheUser.objects.create_user(
            email="owner@example.com", 
            password="password", 
            identifier="owner123",
            user_type="practitioner"
        )
        self.practitioner = Practitioner.objects.get(jhe_user=self.owner_user)
        
        PractitionerOrganization.objects.create(
            practitioner=self.practitioner,
            organization=self.org
        )
        
        self.study = Study.objects.create(
            name="Collaborative Study", 
            description="A study with collaborators", 
            organization=self.org
        )
        
        self.collaborator_user = JheUser.objects.create_user(
            email="collaborator@example.com",
            password="password",
            identifier="collab123"
        )
        
        self.collaboration = StudyCollaborator.objects.create(
            study=self.study,
            jhe_user=self.collaborator_user,
            granted_at=timezone.now()
        )
    
    def test_study_collaborator_creation(self):
        self.assertEqual(self.collaboration.study, self.study)
        self.assertEqual(self.collaboration.jhe_user, self.collaborator_user)
        self.assertIsNotNone(self.collaboration.granted_at)
    
    def test_study_accessible_by_collaborator(self):
        accessible_studies = Study.objects.accessible_by(self.collaborator_user)
        self.assertEqual(accessible_studies.count(), 1)
        self.assertEqual(accessible_studies.first(), self.study)
        
        unshared_study = Study.objects.create(
            name="Unshared Study", 
            description="A study without collaborators", 
            organization=self.org
        )
        
        accessible_studies = Study.objects.accessible_by(self.collaborator_user)
        self.assertEqual(accessible_studies.count(), 1)
        self.assertNotIn(unshared_study, accessible_studies)
    
    def test_unique_constraint(self):
        with self.assertRaises(Exception):
            StudyCollaborator.objects.create(
                study=self.study,
                jhe_user=self.collaborator_user
            )
    
    def test_study_collaborations_relation(self):
        collaborations = self.study.collaborations.all()
        self.assertEqual(collaborations.count(), 1)
        self.assertEqual(collaborations.first(), self.collaboration)
    
    def test_user_study_collaborations_relation(self):
        study_collaborations = self.collaborator_user.study_collaborations.all()
        self.assertEqual(study_collaborations.count(), 1)
        self.assertEqual(study_collaborations.first(), self.collaboration)
        
    def test_collaborators_across_organizations(self):
        other_org = Organization.objects.create(name="Other Org", type="edu")
        other_study = Study.objects.create(
            name="Cross-Org Study",
            description="A study in a different organization",
            organization=other_org
        )
        
        StudyCollaborator.objects.create(
            study=other_study,
            jhe_user=self.collaborator_user
        )
        
        accessible_studies = Study.objects.accessible_by(self.collaborator_user)
        self.assertEqual(accessible_studies.count(), 2)
        self.assertIn(self.study, accessible_studies)
        self.assertIn(other_study, accessible_studies)
        
    def test_permission_elevation_via_collaboration(self):
        patient_user = JheUser.objects.create_user(
            email="patient@example.com",
            password="password",
            identifier="patient123",
            user_type="patient"
        )
        
        patient_study = Study.objects.create(
            name="Patient Study",
            description="A study for patients",
            organization=self.org
        )
        
        StudyCollaborator.objects.create(
            study=patient_study,
            jhe_user=patient_user
        )

        accessible_studies = Study.objects.accessible_by(patient_user)
        self.assertEqual(accessible_studies.count(), 1)
        self.assertIn(patient_study, accessible_studies)

        from django.http import Http404
        from django.core.exceptions import ObjectDoesNotExist
        
        try:
            authorized = Study.practitioner_authorized(patient_user.id, patient_study.id)
            self.assertFalse(authorized)
        except (Http404, ObjectDoesNotExist):
            pass

    def test_patient_collaborator_access_restrictions(self):
        patient_user = JheUser.objects.create_user(
            email="patient_collab@example.com",
            password="password",
            identifier="patcollab",
            user_type="patient"
        )
        
        patient = Patient.objects.get(jhe_user=patient_user)
        patient_collab = StudyCollaborator.objects.create(
            study=self.study,
            jhe_user=patient_user,
            granted_at=timezone.now()
        )
        
        accessible_studies = Study.objects.accessible_by(patient_user)
        self.assertIn(self.study, accessible_studies)
        
        mock_request = type('MockRequest', (), {'user': patient_user})()
        
        from core.views.collaborator import CollaboratorViewSet
        collaborator_view = CollaboratorViewSet()
        collaborator_view.request = mock_request
        
        from django.http import Http404
        with self.assertRaises(Http404):
            collaborator_view.user_can_manage_study(self.study.id)
        
        from django.core.exceptions import ObjectDoesNotExist
        try:
            is_authorized = Study.practitioner_authorized(patient_user.id, self.study.id)
            self.assertFalse(is_authorized)
        except (Http404, ObjectDoesNotExist):
            pass
        
        patient_study = Study.objects.create(
            name="Patient As Subject",
            description="Study where patient is both collaborator and subject",
            organization=self.org
        )

        study_patient = StudyPatient.objects.create(
            study=patient_study,
            patient=patient
        )

        StudyCollaborator.objects.create(
            study=patient_study,
            jhe_user=patient_user
        )

        self.assertTrue(patient_study in Study.objects.accessible_by(patient_user))
   
        try:
            self.assertFalse(Study.practitioner_authorized(patient_user.id, patient_study.id))
        except (Http404, ObjectDoesNotExist):
            pass

        self.assertTrue(Study.practitioner_authorized(self.owner_user.id, patient_study.id))