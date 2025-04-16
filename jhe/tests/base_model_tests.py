from core.models import (
  JheUser, Organization, JheUserOrganization, Patient,
  CodeableConcept, Study, StudyPatient, StudyPatientScopeConsent,
  StudyScopeRequest, DataSource, DataSourceSupportedScope,
  StudyDataSource, Observation, ObservationIdentifier
)
from django.utils import timezone
from django.test import TestCase, override_settings

@override_settings(
    DATABASES={
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }
)

class JheUserTest(TestCase):
  def setUp(self):
    self.user = JheUser.objects.create_user(
      email='test@example.com',
      password='testpassword',
      identifier='test123'
    )
  
  def test_user_creation(self):
    self.assertEqual(self.user.email, 'test@example.com')
    self.assertEqual(self.user.identifier, 'test123')
    self.assertFalse(self.user.email_is_verified)
  
  def test_get_by_ehr_id(self):
    users = JheUser.objects.get_by_ehr_id('test123')
    self.assertEqual(users.count(), 1)
    self.assertEqual(users.first(), self.user)


class OrganizationTest(TestCase):
  def setUp(self):
    self.parent_org = Organization.objects.create(
      name='Parent Organization',
      type='prov'
    )
    self.child_org = Organization.objects.create(
      name='Child Organization',
      type='dept',
      part_of=self.parent_org
    )
  
  def test_organization_creation(self):
    self.assertEqual(self.parent_org.name, 'Parent Organization')
    self.assertEqual(self.parent_org.type, 'prov')
    self.assertIsNone(self.parent_org.part_of)
    
    self.assertEqual(self.child_org.name, 'Child Organization')
    self.assertEqual(self.child_org.type, 'dept')
    self.assertEqual(self.child_org.part_of, self.parent_org)
  
  def test_get_children(self):
    children = Organization.get_children(self.parent_org.id)
    self.assertEqual(len(list(children)), 1)
    self.assertEqual(children[0], self.child_org)


class JheUserOrganizationTest(TestCase):
  def setUp(self):
    self.user = JheUser.objects.create_user(
      email='test@example.com',
      password='testpassword',
      identifier='test123'
    )
    self.organization = Organization.objects.create(
      name='Test Organization',
      type='prov'
    )
    self.user_org = JheUserOrganization.objects.create(
      jhe_user=self.user,
      organization=self.organization
    )
  
  def test_user_organization_creation(self):
    self.assertEqual(self.user_org.jhe_user, self.user)
    self.assertEqual(self.user_org.organization, self.organization)


class PatientTest(TestCase):
  def setUp(self):
    self.user = JheUser.objects.create_user(
      email='patient@example.com',
      password='testpassword',
      identifier='patient123'
    )
    self.organization = Organization.objects.create(
      name='Test Hospital',
      type='prov'
    )
    self.patient = Patient.objects.create(
      jhe_user=self.user,
      organization=self.organization,
      identifier='PAT123',
      name_family='Doe',
      name_given='John',
      birth_date='1990-01-01',
      telecom_phone='555-123-4567'
    )
  
  def test_patient_creation(self):
    self.assertEqual(self.patient.jhe_user, self.user)
    self.assertEqual(self.patient.organization, self.organization)
    self.assertEqual(self.patient.identifier, 'PAT123')
    self.assertEqual(self.patient.name_family, 'Doe')
    self.assertEqual(self.patient.name_given, 'John')
    self.assertEqual(str(self.patient.birth_date), '1990-01-01')
    self.assertEqual(self.patient.telecom_phone, '555-123-4567')
  
  def test_get_patient(self):
    patient = self.user.get_patient()
    self.assertEqual(patient, self.patient)
    
  def test_is_patient(self):
    self.assertTrue(self.user.is_patient())


class CodeableConceptTest(TestCase):
  def setUp(self):
    self.code = CodeableConcept.objects.create(
      coding_system='http://loinc.org',
      coding_code='8480-6',
      text='Systolic Blood Pressure'
    )
  
  def test_codeable_concept_creation(self):
    self.assertEqual(self.code.coding_system, 'http://loinc.org')
    self.assertEqual(self.code.coding_code, '8480-6')
    self.assertEqual(self.code.text, 'Systolic Blood Pressure')


class StudyTest(TestCase):
  def setUp(self):
    self.organization = Organization.objects.create(
      name='Research Organization',
      type='edu'
    )
    self.study = Study.objects.create(
      name='Test Study',
      description='A test study for testing',
      organization=self.organization,
      icon_url='https://example.com/icon.png'
    )
  
  def test_study_creation(self):
    self.assertEqual(self.study.name, 'Test Study')
    self.assertEqual(self.study.description, 'A test study for testing')
    self.assertEqual(self.study.organization, self.organization)
    self.assertEqual(self.study.icon_url, 'https://example.com/icon.png')


class StudyPatientTest(TestCase):
  def setUp(self):
    self.organization = Organization.objects.create(
      name='Research Organization',
      type='edu'
    )
    self.user = JheUser.objects.create_user(
      email='patient@example.com',
      password='testpassword',
      identifier='patient123'
    )
    self.patient = Patient.objects.create(
      jhe_user=self.user,
      organization=self.organization,
      identifier='PAT123',
      name_family='Doe',
      name_given='John',
      birth_date='1990-01-01',
      telecom_phone='555-123-4567'
    )
    self.study = Study.objects.create(
      name='Test Study',
      description='A test study for testing',
      organization=self.organization
    )
    self.study_patient = StudyPatient.objects.create(
      study=self.study,
      patient=self.patient
    )
  
  def test_study_patient_creation(self):
    self.assertEqual(self.study_patient.study, self.study)
    self.assertEqual(self.study_patient.patient, self.patient)


class StudyPatientScopeConsentTest(TestCase):
  def setUp(self):
    self.organization = Organization.objects.create(
      name='Research Organization',
      type='edu'
    )
    self.user = JheUser.objects.create_user(
      email='patient@example.com',
      password='testpassword',
      identifier='patient123'
    )
    self.patient = Patient.objects.create(
      jhe_user=self.user,
      organization=self.organization,
      identifier='PAT123',
      name_family='Doe',
      name_given='John',
      birth_date='1990-01-01',
      telecom_phone='555-123-4567'
    )
    self.study = Study.objects.create(
      name='Test Study',
      description='A test study for testing',
      organization=self.organization
    )
    self.study_patient = StudyPatient.objects.create(
      study=self.study,
      patient=self.patient
    )
    self.code = CodeableConcept.objects.create(
      coding_system='http://loinc.org',
      coding_code='8480-6',
      text='Systolic Blood Pressure'
    )
    self.consent = StudyPatientScopeConsent.objects.create(
      study_patient=self.study_patient,
      scope_actions='rs',
      scope_code=self.code,
      consented=True,
      consented_time=timezone.now()
    )
  
  def test_consent_creation(self):
    self.assertEqual(self.consent.study_patient, self.study_patient)
    self.assertEqual(self.consent.scope_actions, 'rs')
    self.assertEqual(self.consent.scope_code, self.code)
    self.assertTrue(self.consent.consented)
    self.assertIsNotNone(self.consent.consented_time)


class StudyScopeRequestTest(TestCase):
  def setUp(self):
    self.organization = Organization.objects.create(
      name='Research Organization',
      type='edu'
    )
    self.study = Study.objects.create(
      name='Test Study',
      description='A test study for testing',
      organization=self.organization
    )
    self.code = CodeableConcept.objects.create(
      coding_system='http://loinc.org',
      coding_code='8480-6',
      text='Systolic Blood Pressure'
    )
    self.scope_request = StudyScopeRequest.objects.create(
      study=self.study,
      scope_actions='rs',
      scope_code=self.code
    )
  
  def test_scope_request_creation(self):
    self.assertEqual(self.scope_request.study, self.study)
    self.assertEqual(self.scope_request.scope_actions, 'rs')
    self.assertEqual(self.scope_request.scope_code, self.code)


class DataSourceTest(TestCase):
  def setUp(self):
    self.data_source = DataSource.objects.create(
      name='Smart Watch',
      type='personal_device'
    )
  
  def test_data_source_creation(self):
    self.assertEqual(self.data_source.name, 'Smart Watch')
    self.assertEqual(self.data_source.type, 'personal_device')


class DataSourceSupportedScopeTest(TestCase):
  def setUp(self):
    self.data_source = DataSource.objects.create(
      name='Smart Watch',
      type='personal_device'
    )
    self.code = CodeableConcept.objects.create(
      coding_system='http://loinc.org',
      coding_code='8480-6',
      text='Systolic Blood Pressure'
    )
    self.supported_scope = DataSourceSupportedScope.objects.create(
      data_source=self.data_source,
      scope_code=self.code
    )
  
  def test_supported_scope_creation(self):
    self.assertEqual(self.supported_scope.data_source, self.data_source)
    self.assertEqual(self.supported_scope.scope_code, self.code)


class StudyDataSourceTest(TestCase):
  def setUp(self):
    self.organization = Organization.objects.create(
      name='Research Organization',
      type='edu'
    )
    self.study = Study.objects.create(
      name='Test Study',
      description='A test study for testing',
      organization=self.organization
    )
    self.data_source = DataSource.objects.create(
      name='Smart Watch',
      type='personal_device'
    )
    self.study_data_source = StudyDataSource.objects.create(
      study=self.study,
      data_source=self.data_source
    )
  
  def test_study_data_source_creation(self):
    self.assertEqual(self.study_data_source.study, self.study)
    self.assertEqual(self.study_data_source.data_source, self.data_source)


class ObservationTest(TestCase):
  def setUp(self):
    self.organization = Organization.objects.create(
      name='Hospital',
      type='prov'
    )
    self.user = JheUser.objects.create_user(
      email='patient@example.com',
      password='testpassword',
      identifier='patient123'
    )
    self.patient = Patient.objects.create(
      jhe_user=self.user,
      organization=self.organization,
      identifier='PAT123',
      name_family='Doe',
      name_given='John',
      birth_date='1990-01-01',
      telecom_phone='555-123-4567'
    )
    self.code = CodeableConcept.objects.create(
      coding_system='http://loinc.org',
      coding_code='8480-6',
      text='Systolic Blood Pressure'
    )
    self.data_source = DataSource.objects.create(
      name='Blood Pressure Monitor',
      type='personal_device'
    )
    self.observation = Observation.objects.create(
      subject_patient=self.patient,
      codeable_concept=self.code,
      data_source=self.data_source,
      status='final',
      value_attachment_data={'value': 120, 'unit': 'mmHg'}
    )
  
  def test_observation_creation(self):
    self.assertEqual(self.observation.subject_patient, self.patient)
    self.assertEqual(self.observation.codeable_concept, self.code)
    self.assertEqual(self.observation.data_source, self.data_source)
    self.assertEqual(self.observation.status, 'final')
    self.assertEqual(self.observation.value_attachment_data, {'value': 120, 'unit': 'mmHg'})


class ObservationIdentifierTest(TestCase):
  def setUp(self):
    self.organization = Organization.objects.create(
      name='Hospital',
      type='prov'
    )
    self.user = JheUser.objects.create_user(
      email='patient@example.com',
      password='testpassword',
      identifier='patient123'
    )
    self.patient = Patient.objects.create(
      jhe_user=self.user,
      organization=self.organization,
      identifier='PAT123',
      name_family='Doe',
      name_given='John',
      birth_date='1990-01-01',
      telecom_phone='555-123-4567'
    )
    self.code = CodeableConcept.objects.create(
      coding_system='http://loinc.org',
      coding_code='8480-6',
      text='Systolic Blood Pressure'
    )
    self.data_source = DataSource.objects.create(
      name='Blood Pressure Monitor',
      type='personal_device'
    )
    self.observation = Observation.objects.create(
      subject_patient=self.patient,
      codeable_concept=self.code,
      data_source=self.data_source,
      status='final',
      value_attachment_data={'value': 120, 'unit': 'mmHg'}
    )
    self.identifier = ObservationIdentifier.objects.create(
      observation=self.observation,
      system='http://example.org/identifiers',
      value='OBS12345'
    )
  
  def test_identifier_creation(self):
    self.assertEqual(self.identifier.observation, self.observation)
    self.assertEqual(self.identifier.system, 'http://example.org/identifiers')
    self.assertEqual(self.identifier.value, 'OBS12345')