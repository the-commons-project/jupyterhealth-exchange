from django.core.management.base import BaseCommand
from django.db import connection
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import get_random_string
from faker import Faker

from core.models import (
    CodeableConcept,
    DataSource,
    Organization,
    StudyPatientScopeConsent,
    Study,
    PractitionerOrganization,
    StudyPatient,
    Observation,
    JheUser,
    StudyScopeRequest,
)

fake = Faker()


class Command(BaseCommand):
    help = 'Seed the database'

    def handle(self, *args, **options):
        self.stdout.write("Seeding RBAC…")
        with transaction.atomic():
            self.reset_sequences()
            self.generate_superuser()
            self.seed_codeable_concept()
            self.seed_data_source()
            self.seed_berkeley()
            self.seed_ucsf()

        self.stdout.write(self.style.SUCCESS("Seeding complete."))

    @staticmethod
    def us_phone_number():
        return fake.numerify(text="+1-###-###-####")

    @staticmethod
    def reset_sequences(restart_with=10001):
        with connection.cursor() as cursor:
            # Fetching all sequence present in public schema
            cursor.execute("""
                           SELECT c.relname AS sequence_name
                           FROM pg_class c
                                    JOIN pg_namespace n ON n.oid = c.relnamespace
                           WHERE c.relkind = 'S'
                             AND n.nspname = 'public';
                           """)
            seqs = [row[0] for row in cursor.fetchall()]

            for seq in seqs:
                cursor.execute(
                    f"ALTER SEQUENCE {seq} RESTART WITH %s;",
                    [restart_with]
                )

    @staticmethod
    def seed_codeable_concept():
        codes = [
            ('https://w3id.org/openmhealth', 'omh:blood-glucose:4.0', 'Blood glucose'),
            ('https://w3id.org/openmhealth', 'omh:blood-pressure:4.0', 'Blood pressure'),
            ('https://w3id.org/openmhealth', 'omh:body-temperature:3.0', 'Body temperature'),
            ('https://w3id.org/openmhealth', 'omh:heart-rate:2.0', 'Heart Rate'),
            ('https://w3id.org/openmhealth', 'omh:oxygen-saturation:2.0', 'Oxygen saturation'),
            ('https://w3id.org/openmhealth', 'omh:respiratory-rate:2.0', 'Respiratory rate'),
            ('https://w3id.org/openmhealth', 'omh:rr-interval:1.0', 'RR Interval')
        ]
        # bulk create thing
        for system, code, text in codes:
            CodeableConcept.objects.update_or_create(
                coding_system=system,
                coding_code=code,
                text=text,
            )

    def seed_data_source(self):
        data_source = [
            ('CareX', 'personal_device'),
            ('Dexcom', 'personal_device'),
            ('iHealth', 'personal_device')
        ]
        for name, type in data_source:
            DataSource.objects.update_or_create(
                name=name, type=type
            )

    def seed_berkeley(self):

        ucb = Organization.objects.create(name="University of California Berkeley", type="edu", part_of=None)
        ccdss = Organization.objects.create(name="College of Computing, Data Science and Society", type="edu",
                                            part_of=ucb)
        bids = Organization.objects.create(name="Berkeley Institute for Data Science (BIDS)", type="edu", part_of=ccdss)

        mary = self.create_user("mary@example.com")

        manager_links = [
            PractitionerOrganization(
                practitioner=mary,
                organization=org,
                role="manager"
            )
            for org in [ucb, ccdss, bids]
        ]
        PractitionerOrganization.objects.bulk_create(manager_links)

        megan = self.create_user("megan@example.com")
        PractitionerOrganization.objects.create(practitioner=megan, organization=bids, role="member")

        victor = self.create_user("victor@example.com")
        PractitionerOrganization.objects.create(practitioner=victor, organization=bids, role="viewer")

        tom = self.create_user("tom@example.com")
        PractitionerOrganization.objects.create(practitioner=tom, organization=bids, role="viewer")

        # 3) Create BIDS studies
        bp_hr = Study.objects.create(
            name="BIDS Study on BP & HR",
            description="Blood Pressure & Heart Rate",
            organization=bids
        )
        bp = Study.objects.create(
            name="BIDS Study on BP",
            description="Blood Pressure",
            organization=bids
        )

        bp_code = CodeableConcept.objects.get(coding_code='omh:blood-pressure:4.0')
        hr_code = CodeableConcept.objects.get(coding_code='omh:heart-rate:2.0')

        StudyScopeRequest.objects.create(study=bp_hr, scope_code=bp_code)
        StudyScopeRequest.objects.create(study=bp_hr, scope_code=hr_code)
        StudyScopeRequest.objects.create(study=bp, scope_code=bp_code)

        peter = self.create_user("peter@example.com", user_type="patient")
        peter.organizations.add(bids)
        pamela = self.create_user("pamela@example.com", user_type="patient")
        pamela.organizations.add(bids)

        sp_peter_bp_hr = StudyPatient.objects.create(study=bp_hr, patient=peter)
        sp_peter_bp = StudyPatient.objects.create(study=bp, patient=peter)
        sp_pamela_bp_hr = StudyPatient.objects.create(study=bp_hr, patient=pamela)
        sp_pamela_bp = StudyPatient.objects.create(study=bp, patient=pamela)

        now = timezone.now()
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_peter_bp_hr, scope_code=bp_code,
            consented=True, consented_time=now
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_peter_bp_hr, scope_code=hr_code,
            consented=True, consented_time=now
        )

        for sp, codes in [(sp_pamela_bp_hr, [bp_code, hr_code]), (sp_pamela_bp, [bp_code])]:
            for code in codes:
                StudyPatientScopeConsent.objects.create(
                    study_patient=sp, scope_code=code,
                    consented=True, consented_time=now
                )

        for consent in StudyPatientScopeConsent.objects.filter(consented=True):
            scope_code = consent.scope_code
            Observation.objects.create(
                subject_patient=consent.study_patient.patient,
                codeable_concept=scope_code,
                value_attachment_data={scope_code.text: "placeholder"}
            )

    def seed_ucsf(self):

        ucsf = Organization.objects.create(name="University of California San Francisco", type="edu", part_of=None)
        med = Organization.objects.create(name="Department of Medicine", type="edu", part_of=ucsf)
        cardio = Organization.objects.create(name="Cardiology", type="edu", part_of=med)
        mosl = Organization.objects.create(name="Moslehi Lab", type="laboratory", part_of=cardio)
        olgin = Organization.objects.create(name="Olgin Lab", type="laboratory", part_of=cardio)

        mark = self.create_user("mark@example.com", user_type="practitioner")
        practitioner_org_links = [
            PractitionerOrganization(
                practitioner=mark,
                organization=org,
                role="manager"
            )
            for org in [ucsf, med, cardio, mosl]
        ]
        PractitionerOrganization.objects.bulk_create(practitioner_org_links)

        tom = JheUser.objects.get(email="tom@example.com").practitioner
        PractitionerOrganization.objects.create(practitioner=tom, organization=mosl, role="member")
        PractitionerOrganization.objects.create(practitioner=tom, organization=olgin, role="manager")

        rr_code = CodeableConcept.objects.get(coding_code='omh:respiratory-rate:2.0')
        bt_code = CodeableConcept.objects.get(coding_code='omh:body-temperature:3.0')
        o2_code = CodeableConcept.objects.get(coding_code='omh:oxygen-saturation:2.0')

        cardio_rr = Study.objects.create(
            name="Cardio Study on RR",
            description="Respiratory rate",
            organization=cardio
        )
        mosl_bt = Study.objects.create(
            name="Moslehi Study on BT",
            description="Body Temperature",
            organization=mosl
        )
        olgin_o2 = Study.objects.create(
            name="Olgin Study on O2",
            description="Oxygen Saturation",
            organization=olgin
        )

        StudyScopeRequest.objects.create(study=cardio_rr, scope_code=rr_code)
        StudyScopeRequest.objects.create(study=mosl_bt, scope_code=bt_code)
        StudyScopeRequest.objects.create(study=olgin_o2, scope_code=o2_code)

        percy = self.create_user("percy@example.com", user_type="patient")
        percy.organizations.add(mosl)
        paul = self.create_user("paul@example.com", user_type="patient")
        paul.organizations.add(olgin)
        pat = self.create_user("pat@example.com", user_type="patient")
        pat.organizations.add(cardio, olgin)

        sp_percy_bt = StudyPatient.objects.create(study=mosl_bt, patient=percy)
        sp_paul_o2 = StudyPatient.objects.create(study=olgin_o2, patient=paul)
        sp_pat_rr = StudyPatient.objects.create(study=cardio_rr, patient=pat)
        sp_pat_o2 = StudyPatient.objects.create(study=olgin_o2, patient=pat)

        now = timezone.now()

        StudyPatientScopeConsent.objects.create(
            study_patient=sp_percy_bt, scope_code=bt_code,
            consented=True, consented_time=now
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_paul_o2, scope_code=o2_code,
            consented=True, consented_time=now
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_pat_rr, scope_code=rr_code,
            consented=True, consented_time=now
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_pat_o2, scope_code=o2_code,
            consented=True, consented_time=now
        )

        for consent in StudyPatientScopeConsent.objects.filter(consented=True):
            scope_code = consent.scope_code
            Observation.objects.create(
                subject_patient=consent.study_patient.patient,
                codeable_concept=scope_code,
                value_attachment_data={scope_code.text: "placeholder"}
            )

    def create_user(self, email, user_type="practitioner"):
        user = JheUser(email=email, password=get_random_string(length=16),
                       first_name=email.split('@')[0], last_name=fake.last_name(),
                       user_type=user_type)
        user.identifier = f'fhir-{str(user.id)[-1] * 3}'
        user.save()
        if user_type == "practitioner":
            practitioner = user.practitioner_profile
            practitioner.birth_date = fake.date_of_birth(minimum_age=25, maximum_age=45)
            practitioner.telecom_phone = self.us_phone_number()
            practitioner.save()
            return practitioner
        elif user_type == 'patient':
            patient = user.patient_profile
            patient.birth_date = fake.date_of_birth(minimum_age=25, maximum_age=45)
            patient.telecom_phone = self.us_phone_number()
            patient.save()
            return patient
        return None

    @staticmethod
    def generate_superuser(email="sam@example.com"):
        JheUser.objects.create_superuser(
            email=email,
            password="Jhe1234!",
        )
