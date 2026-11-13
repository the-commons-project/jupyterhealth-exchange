import io
import os
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone
from django.utils.crypto import get_random_string
from faker import Faker
from oauth2_provider.models import get_application_model

from core import bootstrap
from core.models import (
    JHE_FHIR_SOURCE_BASE,
    CodeableConcept,
    DataSource,
    EhrBrand,
    EhrBrandLocation,
    EhrVendor,
    FhirAuxResource,
    FhirSource,
    JheClient,
    JheSetting,
    JheUser,
    Observation,
    Organization,
    Patient,
    PatientIdentifier,
    Practitioner,
    PractitionerOrganization,
    Study,
    StudyClient,
    StudyDataSource,
    StudyPatient,
    StudyPatientScopeConsent,
    StudyScopeRequest,
    apply_jhe_extensions,
)
from core.utils import generate_observation_value_attachment_data

fake = Faker()

SLEEP_EPISODE_CODE = "ieee:sleep-episode:1.0"


def sleep_episode_data_point(start, hours_asleep, awakenings):
    """A schema-valid ieee:sleep-episode:1.0 data point.

    Built by hand rather than via generate_observation_value_attachment_data: there is no
    example data point on disk for this code, and the generator's fallback writes an
    ``effective_time_frame.date_time``, whereas sleep-episode requires a ``time_interval``.
    """
    end = start + timedelta(hours=hours_asleep)
    return {
        "header": {
            "uuid": str(uuid.uuid4()),
            "schema_id": {"namespace": "ieee", "name": "sleep-episode", "version": "1.0"},
            "source_creation_date_time": end.isoformat().replace("+00:00", "Z"),
            "modality": "sensed",
            "acquisition_provenance": {"source_name": "Oura"},
        },
        "body": {
            "effective_time_frame": {
                "time_interval": {
                    "start_date_time": start.isoformat().replace("+00:00", "Z"),
                    "end_date_time": end.isoformat().replace("+00:00", "Z"),
                }
            },
            "total_sleep_time": {"value": round(hours_asleep * 3600), "unit": "sec"},
            "number_of_awakenings": awakenings,
            "is_main_sleep": True,
        },
    }


class Command(BaseCommand):
    help = "Seed the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush-db",
            action="store_true",
            help="Flush the entire database before seeding as already seeding won't work with already populated DB.",
        )
        # Usage: python manage.py seed --flush-db --with-rich-demo
        parser.add_argument(
            "--with-rich-demo",
            action="store_true",
            help="After the base seed, also generate the synthetic CGM + Oura demo cohort.",
        )

    # -- uniform step reporting ------------------------------------------------------
    #
    # Every step reports what it wrote in the same shape. Counts are *measured* -- row
    # counts diffed around the call -- rather than tallied by hand inside each step. A
    # hand-written total goes stale the moment a step starts writing a new model, which
    # is how this command came to report three of its dozen steps and nothing else.

    _STEP_LABEL_WIDTH = 26

    # Labels come from each model's Meta, which gets a few of them wrong for display: Django
    # pluralises Study as "studys", and the JHE-prefixed models read as lowercase "jhe ...".
    # Overriding here keeps it to the seed output rather than an AlterModelOptions migration
    # (which would also change the admin -- a separate call).
    _ROW_LABELS = {
        "study": ("study", "studies"),
        "jhe setting": ("JHE setting", "JHE settings"),
        "jhe client": ("JHE client", "JHE clients"),
    }

    def _step(self, label, models, fn, *args, **kwargs):
        """Run one seeding step, then report the rows it added on a single aligned line."""
        self._skip_reason = None
        before = [model.objects.count() for model in models]
        result = fn(*args, **kwargs)
        added = [(model, model.objects.count() - count) for model, count in zip(models, before)]

        if self._skip_reason:
            detail = self.style.WARNING(f"skipped — {self._skip_reason}")
        else:
            counts = [self._row_count(model, n) for model, n in added if n]
            detail = ", ".join(counts) if counts else "no new rows"
        self.stdout.write(f"  {label.ljust(self._STEP_LABEL_WIDTH)}{detail}")
        return result

    def _skip(self, reason):
        """Called by a step that deliberately did nothing, so _step can report why."""
        self._skip_reason = reason

    @classmethod
    def _row_count(cls, model, n):
        meta = model._meta
        singular, plural = cls._ROW_LABELS.get(
            str(meta.verbose_name), (str(meta.verbose_name), str(meta.verbose_name_plural))
        )
        return f"{n} {singular if n == 1 else plural}"

    def handle(self, *args, **options):
        self.stdout.write("Seeding RBAC…")
        if options["flush_db"]:
            self.stdout.write("Flushing the database…")
            call_command("flush", "--noinput")

        Application = get_application_model()
        with transaction.atomic():
            self.reset_sequences()
            self._step("superuser", (JheUser, Practitioner), self.generate_superuser)
            self._step(
                "bootstrap",
                (JheSetting, CodeableConcept, DataSource, Application, JheClient, Organization, EhrVendor),
                self.run_bootstrap,
            )
            # The development values of the settings, over bootstrap's neutral ones.
            self._step("settings", (JheSetting,), self.seed_jhe_settings)
            root_organization = Organization.objects.get(id=bootstrap.ROOT_ORGANIZATION_ID)
            self._step(
                "example institute",
                (
                    Organization,
                    JheUser,
                    Patient,
                    Study,
                    Observation,
                    FhirSource,
                    FhirAuxResource,
                    EhrVendor,
                    EhrBrand,
                    EhrBrandLocation,
                ),
                self.seed_example_institute,
                root_organization,
            )
            self._step(
                "health system",
                (Organization, JheUser, Patient, Study, Observation),
                self.seed_health_system,
                root_organization,
            )
            self._step("MCP broker client", (Application,), self.seed_mcp_broker_application)
            self._step("SMART EHR launch client", (Application,), self.seed_sof_ehr_launch_application)

        if options.get("with_rich_demo"):
            # Its own summary line (the CGM/wearable split) is suppressed here so the seed
            # output stays uniform; run `manage.py seed_rich_demo` directly to see it.
            self._step(
                "rich demo (CGM + Oura)",
                (Patient, Observation),
                call_command,
                "seed_rich_demo",
                stdout=io.StringIO(),
            )

        self.stdout.write(self.style.SUCCESS("Seeding complete."))

    def seed_jhe_settings(self):
        invite_code = "jhe"

        jhe_settings = [
            # Honor the SITE_URL env so the seeded host matches where JHE is actually
            # served (e.g. http://localhost:8001 when running JHE itself on a non-default
            # port for Open Wearables testing). Defaults to http://localhost:8000 when
            # SITE_URL is unset, preserving prior behavior. Invitation links embed this
            # host, so a mismatch sends redemption to the wrong server.
            ("site.url", "string", settings.SITE_URL),
            ("site.ui.title", "string", "JupyterHealth Exchange"),
            ("site.ui.logo", "string", ""),  # static path of a deployment's own logo for patient-facing pages
            ("site.ui.theme_css", "string", ""),  # static path of a stylesheet overriding the --pf-* tokens/fonts
            ("site.time_zone", "string", "America/Los_Angeles"),
            ("site.registration_invite_code", "string", invite_code),
            ("auth.default_orgs", "string", ""),  # "20001:viewer;20002:member"
            # IdP config is a SocialApp row (Django admin); these only gate the
            # login button and restrict signup domains.
            ("auth.sso.saml2", "int", 0),
            ("auth.sso.valid_domains", "string", ""),
            ("auth.patient.invitation_expiration_days", "int", 7),
            ("auth.patient.invitation_redemption_window_hours", "int", 12),
            # OAuth client_ids whose auth-code login uses the email one-time-code
            # flow (/accounts/login-otp/) instead of the password form.
            ("auth.patient_access_clients", "json", []),
            # SMART on FHIR EHR-launch token exchange (/o/token-exchange).
            # trusted_issuers: EHR OIDC issuers (id_token `iss`) whose id_tokens we
            # accept; JWKS is discovered from each. trusted_audience: the SMART app's
            # client_id registered at the EHR (the id_token `aud`).
            # NB: the issuer is the id_token's literal `iss` — per standard OIDC this
            # is the EHR's OAuth server, NOT its FHIR base (verified live against the
            # Epic sandbox 2026-07-12; MedPlum is the outlier whose iss is the FHIR
            # base URL with a trailing slash). Docs: https://jupyterhealth.github.io/software-documentation/jhe/provider-ehr-launch
            (
                "auth.sof.trusted_issuers",
                "json",
                ["https://fhir.epic.com/interconnect-fhir-oauth/oauth2"],
            ),
            ("auth.sof.trusted_audience", "string", "77849e74-8e2a-4c2f-826c-bdbef6da3357"),
            # Open Wearables polling pipeline (see ow_poll management command).
            ("module.ow", "bool", False),
            ("ow.sync_in_progress", "string", ""),
            # Open Wearables connection config (moved from env). api_key is a secret —
            # left blank in the seed for an operator to fill in; api_url defaults to the
            # local OW dev server. Consumed at runtime via get_setting() in ow.py /
            # patient.py / ow_poll.
            ("ow.api_url", "string", "http://localhost:8001"),
            ("ow.api_key", "string", ""),
            ("ow.poll_window_days", "int", 1),
            ("ow.sleep_lookback_days", "int", 7),
        ]
        for key, value_type, value in jhe_settings:
            setting, _ = JheSetting.objects.update_or_create(
                key=key,
                defaults={"value_type": value_type},
            )
            setting.set_value(value_type, str(value) if value_type == "int" else value)
            setting.save()

    @staticmethod
    def us_phone_number():
        return fake.numerify(text="+1-###-###-####")

    @staticmethod
    def reset_sequences(restart_with=10001):
        with connection.cursor() as cursor:
            seqs = [
                "core_jheuser_id_seq",
                "core_organization_id_seq",
                "core_study_id_seq",
                "core_patient_id_seq",
                "core_codeableconcept_id_seq",
                "core_observation_id_seq",
                "core_datasource_id_seq",
                "core_practitioner_id_seq",
                "core_fhirsource_id_seq",
            ]

            for seq in seqs:
                cursor.execute(f"ALTER SEQUENCE {seq} RESTART WITH %s;", [restart_with])
                restart_with = restart_with + 10000

            cursor.execute("ALTER SEQUENCE core_patientinvitation_id_seq RESTART WITH 101;")

    # The reference data lives in core.bootstrap (`manage.py bootstrap`): the seed runs it
    # first and layers the demo on top. The single steps are kept as methods because tests
    # call them by name.
    @staticmethod
    def run_bootstrap():
        from core.management.commands.bootstrap import Command as Bootstrap

        for _label, step in Bootstrap.STEPS:
            step()

    @staticmethod
    def seed_codeable_concepts():
        bootstrap.ensure_codeable_concepts()

    @staticmethod
    def seed_data_sources():
        bootstrap.ensure_data_sources()

    @staticmethod
    def seed_clients():
        bootstrap.ensure_clients()

    @staticmethod
    def create_root_organization():
        bootstrap.ensure_root_organization()
        return Organization.objects.get(id=bootstrap.ROOT_ORGANIZATION_ID)

    @staticmethod
    def seed_ehr_vendor_sandbox_config():
        """Fill in the Epic Sandbox vendor's OAuth config so the end-to-end connect flow (#489)
        works out of the box against Epic's sandbox brand. Fill-gap only, like the deployment
        client_id this replaced: an operator's edits via the EHRs admin page must survive a
        reseed. Epic Production is left blank -- a production Epic app needs Epic's own
        certification/registration process, which seed can't do for an operator."""
        epic_vendor = EhrVendor.objects.filter(name="Epic Sandbox").first()
        if epic_vendor and not epic_vendor.ehr_client_id:
            # Non-production client id of the Epic app "JupyterHealth Exchange - USCDI v3"
            # (appId 55446). (Same Epic sandbox app/client id used in the Phase 1 POC.)
            # NOTE: this app's registered redirect URIs at fhir.epic.com must include
            # http://localhost:8000/clients/ehr-patient-portal/callback (plus the
            # jhe.fly.dev equivalent) to match the invitation_url above -- update it there
            # if it still only has the old :8001 callback registered.
            epic_vendor.ehr_client_id = "77849e74-8e2a-4c2f-826c-bdbef6da3357"
            epic_vendor.supported_scopes = " ".join(EhrVendor.SUPPORTED_SCOPES)
            epic_vendor.save()

    def seed_example_institute(self, root_organization):
        planetary_research_institute = Organization.objects.create(
            name="Planetary Research Institute",
            type="edu",
            part_of=root_organization,
        )
        saturn_school_of_data_science = Organization.objects.create(
            name="Saturn School of Data Science",
            type="team",
            part_of=planetary_research_institute,
        )
        lifespan_lab = Organization.objects.create(
            name="Lifespan Lab", type="laboratory", part_of=saturn_school_of_data_science
        )

        manager_mary = self.create_user_with_profile("manager_mary@example.com")

        manager_links = [
            PractitionerOrganization(practitioner=manager_mary, organization=org, role="manager")
            for org in [planetary_research_institute, saturn_school_of_data_science, lifespan_lab]
        ]
        PractitionerOrganization.objects.bulk_create(manager_links)

        member_megan = self.create_user_with_profile("member_megan@example.com")
        PractitionerOrganization.objects.create(practitioner=member_megan, organization=lifespan_lab, role="member")

        viewer_victor = self.create_user_with_profile("viewer_victor@example.com")
        PractitionerOrganization.objects.create(practitioner=viewer_victor, organization=lifespan_lab, role="viewer")

        three_org_tom = self.create_user_with_profile("three_org_tom@example.com")
        PractitionerOrganization.objects.create(practitioner=three_org_tom, organization=lifespan_lab, role="viewer")

        lifespan_study_bp_hr = Study.objects.create(
            name="Lifespan Study on BP & HR",
            description="Blood Pressure & Heart Rate",
            organization=lifespan_lab,
        )
        lifespan_study_sleep_bp = Study.objects.create(
            name="Lifespan Study on Sleep & BP",
            description="Sleep & Blood Pressure",
            organization=lifespan_lab,
        )

        code_omh_bp = CodeableConcept.objects.get(coding_code="omh:blood-pressure:4.0")
        code_omh_hr = CodeableConcept.objects.get(coding_code="omh:heart-rate:2.0")
        code_ieee_sleep = CodeableConcept.objects.get(coding_code=SLEEP_EPISODE_CODE)
        code_fhir_qr = CodeableConcept.objects.get(coding_code="QuestionnaireResponse")
        code_fhir_star = CodeableConcept.objects.get(
            coding_system="http://hl7.org/fhir/resource-types", coding_code="*"
        )

        StudyScopeRequest.objects.create(study=lifespan_study_bp_hr, scope_code=code_omh_bp)
        StudyScopeRequest.objects.create(study=lifespan_study_bp_hr, scope_code=code_omh_hr)
        StudyScopeRequest.objects.create(study=lifespan_study_bp_hr, scope_code=code_fhir_qr)
        StudyScopeRequest.objects.create(study=lifespan_study_sleep_bp, scope_code=code_omh_bp)
        StudyScopeRequest.objects.create(study=lifespan_study_sleep_bp, scope_code=code_ieee_sleep)
        StudyScopeRequest.objects.create(study=lifespan_study_sleep_bp, scope_code=code_fhir_qr)

        carex_ds = DataSource.objects.get(name="CareX")
        questionnaire_ds = DataSource.objects.get(name="Questionnaire")
        carex_client = get_application_model().objects.get(name="CareX")
        for study in [lifespan_study_bp_hr, lifespan_study_sleep_bp]:
            StudyDataSource.objects.create(study=study, data_source=carex_ds)
            StudyDataSource.objects.create(study=study, data_source=questionnaire_ds)
            StudyClient.objects.create(study=study, client=carex_client)

        # The sleep study collects its sleep episodes from Oura via the Open Wearables client.
        oura_ds = DataSource.objects.get(name="Oura")
        ow_client = get_application_model().objects.get(name="Open Wearables")
        StudyDataSource.objects.create(study=lifespan_study_sleep_bp, data_source=oura_ds)
        StudyClient.objects.create(study=lifespan_study_sleep_bp, client=ow_client)

        # Wire the EHR Patient Portal (Epic SMART) client to Peter's BP & HR study so it
        # shows up as an invitable client for him (consolidated_clients gates on StudyClient).
        # This makes the EHR patient-portal end-to-end flow (#489) testable out of the box.
        ehr_patient_portal_client = get_application_model().objects.get(name="EHR Patient Portal")
        StudyClient.objects.create(study=lifespan_study_bp_hr, client=ehr_patient_portal_client)

        # Also requests clinical records via the EHR Patient Portal; left pending so the demo starts unconsented.
        ehr_patient_portal_ds = DataSource.objects.get(name="EHR Patient Portal")
        StudyDataSource.objects.create(study=lifespan_study_bp_hr, data_source=ehr_patient_portal_ds)
        StudyScopeRequest.objects.create(study=lifespan_study_bp_hr, scope_code=code_fhir_star)

        # uscdi-patient-access: invitable for the same study, under the same records request.
        uscdi_client = get_application_model().objects.get(name="USCDI Patient Access")
        StudyClient.objects.create(study=lifespan_study_bp_hr, client=uscdi_client)
        uscdi_ds = DataSource.objects.get(name="USCDI Patient Access")
        StudyDataSource.objects.create(study=lifespan_study_bp_hr, data_source=uscdi_ds)

        # Seed EHR hospital brands so the EHR Patient Portal hospital picker (#489) has data to
        # search out of the box. Uses the curated sample bundle; production imports the
        # full Epic file via `manage.py import_ehr_brands --file <download>`.
        call_command("import_ehr_brands", stdout=io.StringIO())
        self.seed_ehr_vendor_sandbox_config()

        ll_patient_pete = self.create_user_with_profile("ll_patient_peter@example.com", user_type="patient")
        ll_patient_pete.organizations.add(lifespan_lab)
        # Example multi-identifier demo data (see PR #409 / ghi-352).
        PatientIdentifier.objects.create(
            patient=ll_patient_pete, system="https://openwearables.io/ns/patient-id", value="abc123"
        )
        PatientIdentifier.objects.create(patient=ll_patient_pete, system="urn:oid:1.2.3.4", value="E1234")
        ll_patient_pamela = self.create_user_with_profile("ll_patient_pamela@example.com", user_type="patient")
        ll_patient_pamela.organizations.add(lifespan_lab)

        sp_peter_bp_hr = StudyPatient.objects.create(study=lifespan_study_bp_hr, patient=ll_patient_pete)
        sp_peter_sleep_bp = StudyPatient.objects.create(study=lifespan_study_sleep_bp, patient=ll_patient_pete)
        sp_pamela_bp_hr = StudyPatient.objects.create(study=lifespan_study_bp_hr, patient=ll_patient_pamela)
        sp_pamela_sleep_bp = StudyPatient.objects.create(study=lifespan_study_sleep_bp, patient=ll_patient_pamela)

        now = timezone.now()
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_peter_bp_hr,
            scope_code=code_omh_bp,
            consented=True,
            consented_time=now,
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_peter_bp_hr,
            scope_code=code_omh_hr,
            consented=True,
            consented_time=now,
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_peter_bp_hr,
            scope_code=code_fhir_qr,
            consented=True,
            consented_time=now,
        )

        for sp, codes in [
            (sp_pamela_bp_hr, [code_omh_bp, code_omh_hr, code_fhir_qr]),
            (sp_pamela_sleep_bp, [code_omh_bp, code_ieee_sleep, code_fhir_qr]),
            (sp_peter_sleep_bp, [code_ieee_sleep]),
        ]:
            for code in codes:
                StudyPatientScopeConsent.objects.create(
                    study_patient=sp,
                    scope_code=code,
                    consented=True,
                    consented_time=now,
                )

        planetary_research_institute_study_patients = [
            sp_peter_bp_hr,
            sp_peter_sleep_bp,
            sp_pamela_bp_hr,
            sp_pamela_sleep_bp,
        ]
        for consent in (
            StudyPatientScopeConsent.objects.filter(
                consented=True, study_patient__in=planetary_research_institute_study_patients
            )
            .exclude(scope_code__coding_system__startswith="http://hl7.org/fhir/")
            # Seeded explicitly below: generate_observation_value_attachment_data has no
            # example for this code and its fallback body fails schema validation.
            .exclude(scope_code__coding_code=SLEEP_EPISODE_CODE)
        ):
            scope_code = consent.scope_code
            Observation.objects.create(
                subject_patient=consent.study_patient.patient,
                codeable_concept=scope_code,
                omh_data=generate_observation_value_attachment_data(consent.scope_code.coding_code),
            )

        # A couple of nights each, from the Oura data source the sleep study collects through.
        for patient, nights in [
            (ll_patient_pete, [(2, 7.5, 1), (1, 6.25, 3)]),
            (ll_patient_pamela, [(2, 8.0, 0), (1, 5.75, 2)]),
        ]:
            for days_ago, hours_asleep, awakenings in nights:
                bedtime = (now - timedelta(days=days_ago)).replace(hour=23, minute=0, second=0, microsecond=0)
                Observation.objects.create(
                    subject_patient=patient,
                    codeable_concept=code_ieee_sleep,
                    data_source=oura_ds,
                    omh_data=sleep_episode_data_point(bedtime, hours_asleep, awakenings),
                )

        peter_fhir_source = FhirSource.objects.create(
            patient=ll_patient_pete,
            data_source=questionnaire_ds,
            label="Records from Halo Health System",
        )
        pamela_fhir_source = FhirSource.objects.create(
            patient=ll_patient_pamela,
            data_source=questionnaire_ds,
            label="Chart from Comet Clinic",
        )

        peter_patient_fhir_id = "a1b2c3d4e5f6"
        pamela_patient_fhir_id = "b2c3d4e5f6a1"

        peter_qr_resources = [
            ("c3d4e5f6a1b2", "2025-10-01", 42, 18, 65),
            ("d4e5f6a1b2c3", "2025-10-08", 55, 30, 72),
            ("e5f6a1b2c3d4", "2025-10-15", 38, 22, 48),
        ]
        pamela_qr_resources = [
            ("f6a1b2c3d4e5", "2025-10-03", 60, 45, 80),
            ("a2b3c4d5e6f7", "2025-10-10", 50, 35, 70),
            ("b3c4d5e6f7a2", "2025-10-17", 25, 10, 40),
            ("c4d5e6f7a2b3", "2025-10-24", 70, 55, 90),
        ]

        for fhir_source, patient_fhir_id, resources in [
            (peter_fhir_source, peter_patient_fhir_id, peter_qr_resources),
            (pamela_fhir_source, pamela_patient_fhir_id, pamela_qr_resources),
        ]:
            for fhir_resource_id, authored, cough, dyspnea, fatigue in resources:
                fhir_data = apply_jhe_extensions(
                    {
                        "resourceType": "QuestionnaireResponse",
                        "status": "completed",
                        "questionnaire": "Questionnaire/weekly-symptom-severity-vas",
                        "subject": {"reference": f"Patient/{patient_fhir_id}"},
                        "authored": authored,
                        "item": [
                            {"linkId": "cough-severity", "answer": [{"valueInteger": cough}]},
                            {"linkId": "dyspnea-severity", "answer": [{"valueInteger": dyspnea}]},
                            {"linkId": "fatigue-severity", "answer": [{"valueInteger": fatigue}]},
                        ],
                    },
                    fhir_source,
                )
                FhirAuxResource.objects.create(
                    fhir_source=fhir_source,
                    resource_type="QuestionnaireResponse",
                    patient_fhir_id=patient_fhir_id,
                    fhir_resource_id=fhir_resource_id,
                    fhir_data=fhir_data,
                )

        for practitioner in [manager_mary, member_megan, viewer_victor]:
            practitioner.save_setting("current_organization_id", lifespan_lab.id)
            practitioner.save_setting("current_study_id", lifespan_study_bp_hr.id)
            # Open the FHIR browser on the QuestionnaireResponses seeded above, so the page has
            # rows on a first visit instead of an empty JHE-native store. These are the same
            # settings the browser writes back via X-JHE-Remember-View (FHIRResourceView.
            # _remember_search). The source value is the "External" option: JHE_FHIR_SOURCE_BASE
            # with the trailing slash the `_source:below` prefix search uses -- the browser
            # normalizes away anything that is not exactly that string. The JHE Patient ID box is
            # deliberately left unset: an absent current_fhir_jhe_patient_id is a cleared filter.
            practitioner.save_setting("current_fhir_resource", "QuestionnaireResponse")
            practitioner.save_setting("current_fhir_source", f"{JHE_FHIR_SOURCE_BASE}/")

    def seed_health_system(self, root_organization):
        nhs = Organization.objects.create(
            name="Neptune Health System",
            type="prov",
            part_of=root_organization,
        )
        department_of_medicine = Organization.objects.create(name="Department of Medicine", type="dept", part_of=nhs)
        cardiology_division = Organization.objects.create(
            name="Cardiology Division", type="dept", part_of=department_of_medicine
        )
        neptunian_pulse_lab = Organization.objects.create(
            name="Neptunian Pulse Lab", type="laboratory", part_of=cardiology_division
        )
        cosmic_cardio_lab = Organization.objects.create(
            name="Cosmic Cardio Lab", type="laboratory", part_of=cardiology_division
        )

        manager_mark = self.create_user_with_profile("manager_mark@example.com", user_type="practitioner")
        practitioner_org_links = [
            PractitionerOrganization(practitioner=manager_mark, organization=org, role="manager")
            for org in [nhs, department_of_medicine, cardiology_division, neptunian_pulse_lab]
        ]
        PractitionerOrganization.objects.bulk_create(practitioner_org_links)

        three_org_tom = JheUser.objects.get(email="three_org_tom@example.com").practitioner
        PractitionerOrganization.objects.create(
            practitioner=three_org_tom, organization=neptunian_pulse_lab, role="member"
        )
        PractitionerOrganization.objects.create(
            practitioner=three_org_tom, organization=cosmic_cardio_lab, role="manager"
        )

        code_omh_bg = CodeableConcept.objects.get(coding_code="omh:blood-glucose:4.0")
        code_omh_bt = CodeableConcept.objects.get(coding_code="omh:body-temperature:4.0")
        code_omh_o2 = CodeableConcept.objects.get(coding_code="omh:oxygen-saturation:2.0")

        cardio_bgl = Study.objects.create(
            name="Cardiology Div Study on BGL",
            description="Blood Glucose",
            organization=cardiology_division,
        )
        neptunian_pulse_lab_bt = Study.objects.create(
            name="Nep Pulse Lab Study on BT",
            description="Body Temperature",
            organization=neptunian_pulse_lab,
        )
        cosmic_cardio_lab_o2 = Study.objects.create(
            name="Cosmic Cardio Lab Study on O2",
            description="Oxygen Saturation",
            organization=cosmic_cardio_lab,
        )

        StudyScopeRequest.objects.create(study=cardio_bgl, scope_code=code_omh_bg)
        StudyScopeRequest.objects.create(study=neptunian_pulse_lab_bt, scope_code=code_omh_bt)
        StudyScopeRequest.objects.create(study=cosmic_cardio_lab_o2, scope_code=code_omh_o2)

        commonhealth_client = get_application_model().objects.get(name="CommonHealth")
        StudyDataSource.objects.create(study=neptunian_pulse_lab_bt, data_source=DataSource.objects.get(name="iHealth"))
        StudyClient.objects.create(study=neptunian_pulse_lab_bt, client=commonhealth_client)
        StudyDataSource.objects.create(study=cardio_bgl, data_source=DataSource.objects.get(name="Dexcom Stelo"))
        StudyClient.objects.create(study=cardio_bgl, client=commonhealth_client)

        neptunian_pulse_lab_patient_percy = self.create_user_with_profile(
            "npl_patient_percy@example.com", user_type="patient"
        )
        neptunian_pulse_lab_patient_percy.organizations.add(neptunian_pulse_lab)
        neptunian_pulse_lab_ccl_patient_paul = self.create_user_with_profile(
            "ccl_patient_paul@example.com", user_type="patient"
        )
        neptunian_pulse_lab_ccl_patient_paul.organizations.add(cosmic_cardio_lab)
        ccl_cardio_patient_pat = self.create_user_with_profile(
            "ccl_cardio_patient_pat@example.com", user_type="patient"
        )
        ccl_cardio_patient_pat.organizations.add(cardiology_division, cosmic_cardio_lab)

        sp_percy_bt = StudyPatient.objects.create(
            study=neptunian_pulse_lab_bt, patient=neptunian_pulse_lab_patient_percy
        )
        sp_paul_o2 = StudyPatient.objects.create(
            study=cosmic_cardio_lab_o2, patient=neptunian_pulse_lab_ccl_patient_paul
        )
        sp_pat_bg = StudyPatient.objects.create(study=cardio_bgl, patient=ccl_cardio_patient_pat)
        sp_pat_o2 = StudyPatient.objects.create(study=cosmic_cardio_lab_o2, patient=ccl_cardio_patient_pat)

        now = timezone.now()

        StudyPatientScopeConsent.objects.create(
            study_patient=sp_percy_bt,
            scope_code=code_omh_bt,
            consented=True,
            consented_time=now,
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_paul_o2,
            scope_code=code_omh_o2,
            consented=True,
            consented_time=now,
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_pat_bg,
            scope_code=code_omh_bg,
            consented=True,
            consented_time=now,
        )
        StudyPatientScopeConsent.objects.create(
            study_patient=sp_pat_o2,
            scope_code=code_omh_o2,
            consented=True,
            consented_time=now,
        )

        med_study_patients = [sp_percy_bt, sp_paul_o2, sp_pat_bg, sp_pat_o2]
        for consent in StudyPatientScopeConsent.objects.filter(
            consented=True, study_patient__in=med_study_patients
        ).exclude(scope_code__coding_system__startswith="http://hl7.org/fhir/"):
            scope_code = consent.scope_code
            Observation.objects.create(
                subject_patient=consent.study_patient.patient,
                codeable_concept=scope_code,
                omh_data=generate_observation_value_attachment_data(consent.scope_code.coding_code),
            )

        manager_mark.save_setting("current_organization_id", neptunian_pulse_lab.id)
        manager_mark.save_setting("current_study_id", neptunian_pulse_lab_bt.id)
        three_org_tom.save_setting("current_organization_id", cosmic_cardio_lab.id)
        three_org_tom.save_setting("current_study_id", cosmic_cardio_lab_o2.id)

    @staticmethod
    def seed_oauth_application():
        bootstrap.ensure_admin_ui_application()

    def seed_mcp_broker_application(self):
        """The MCP server's confidential client, from MCP_OAUTH_CLIENT_ID/SECRET; skipped when
        they are not set (local and CI seeds that do not run the broker)."""
        if bootstrap.ensure_mcp_broker_application() is None:
            self._skip("MCP_OAUTH_CLIENT_ID/SECRET not set")

    def seed_sof_ehr_launch_application(self):
        """Seed the "SoF EHR Launch" confidential OAuth client used by /o/token-exchange.

        An external SMART on FHIR app authenticates to the token-exchange endpoint
        with this client's id + secret, then exchanges an EHR-issued id_token for a
        JHE user-bound access token linked to this Application. The grant type is
        recorded as ``client-credentials`` because the app authenticates directly with
        id+secret at the token endpoint (no browser redirect); the issued token is
        nonetheless user-bound (docs: https://jupyterhealth.github.io/software-documentation/jhe/provider-ehr-launch).

        The seeded credentials are DEV-ONLY placeholders — rotate them for any real
        deployment (or override via SOF_EHR_LAUNCH_CLIENT_ID / _SECRET). DOT hashes the
        plaintext secret on save (hash_client_secret=True); the app sends the matching
        plaintext.
        """
        client_id = os.environ.get("SOF_EHR_LAUNCH_CLIENT_ID", "sof-ehr-launch")
        client_secret = os.environ.get("SOF_EHR_LAUNCH_CLIENT_SECRET", "sof-ehr-launch-dev-secret")
        get_application_model().objects.update_or_create(
            name="SoF EHR Launch",
            defaults={
                "client_id": client_id,
                "client_secret": client_secret,
                "hash_client_secret": True,
                "client_type": "confidential",
                "authorization_grant_type": "client-credentials",
                "redirect_uris": "",
                "skip_authorization": True,
                "algorithm": "RS256",
                "user_id": None,
            },
        )

    def create_user_with_profile(self, email, user_type="practitioner", password="Jhe1234!"):
        user = JheUser.objects.create_user(
            email=email,
            password=password or get_random_string(length=16),
            first_name=email.split("@")[0].replace("_", " ").title().replace(" ", ""),
            last_name=fake.last_name(),
            user_type=user_type,
        )
        if user_type == "practitioner":
            practitioner = user.practitioner_profile
            practitioner.birth_date = fake.date_of_birth(minimum_age=25, maximum_age=45)
            practitioner.save()
            return practitioner
        elif user_type == "patient":
            patient = user.patient_profile
            patient.birth_date = fake.date_of_birth(minimum_age=25, maximum_age=45)
            patient.telecom_phone = self.us_phone_number()
            patient.save()
            return patient
        return None

    @staticmethod
    def generate_superuser(email="admin@example.com", password="Jhe1234!"):
        JheUser.objects.create_superuser(
            email=email,
            password=password,
        )
