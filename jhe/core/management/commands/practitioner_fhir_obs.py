import requests
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.management.base import BaseCommand

from core.models import Organization, Study, JheUser


class Command(BaseCommand):
    help = "Practitioner user upload patient observations"

    def __init__(self, stdout=None, stderr=None, no_color=False, force_color=False):
        self.session = requests.Session()
        self.BASE_URL = settings.SITE_URL
        super().__init__(stdout, stderr, no_color, force_color)

    def set_auth_token(self, access_token):
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
            }
        )

    def remove_auth_token(self):
        self.session.headers.pop("Authorization")

    def get_tokens(self, code):
        response = self.session.post(
            url=self.BASE_URL + "/o/token/",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.BASE_URL + "/auth/callback",
                "client_id": settings.OIDC_CLIENT_ID,
                "code_verifier": settings.PATIENT_AUTHORIZATION_CODE_VERIFIER,
            },
        ).json()
        return response.get("access_token"), response.get("refresh_token")

    def handle(self, *args, **options):
        # assuming that there's already a signed-up JHE practitioner user "obs-upload@example.com"
        # with password Jhe1234!
        email, password = "obs-upload@example.com", "Jhe1234!"

        # Assuming there's an Organization "Obs Upload Org"
        # Assuming there's a Study with "Obs Upload Study" with iHealth data source and blood glucose scope
        organization_name, study_name = "Obs Upload Org", "Obs Upload Study"

        patient_email = "obs-upload-pat1@example.com"  # register this patient into the organization

        user = authenticate(None, **{"email": email, "password": password})

        grant = user.create_authorization_code(1, settings.OIDC_CLIENT_REDIRECT_URI)

        practitioner_access_token, practitioner_refresh_token = self.get_tokens(grant.code)

        organization = Organization.objects.filter(name=organization_name).first()

        self.set_auth_token(practitioner_access_token)

        # patient creation

        patient_lookup_response = self.session.get(
            url=self.BASE_URL + "/api/v1/patients/global_lookup", params={"email": patient_email}
        ).json()
        if patient_lookup_response:
            return f"patient with Email: {patient_email} already exists."

        patient_creation_response = self.session.post(
            url=self.BASE_URL + "/api/v1/patients",
            json={
                "organizationId": organization.id,
                "identifier": "23455",
                "nameFamily": "jake",
                "nameGiven": "doe",
                "birthDate": "2024-11-02",
                "telecomEmail": patient_email,
                "telecomPhone": "+1-202-555-0342",
            },
        ).json()

        # Add patient to study

        study = Study.objects.filter(name=study_name).first()

        patient_study_response = self.session.post(  # noqa
            url=self.BASE_URL + f"/api/v1/studies/{study.id}/patients",
            json={"patientIds": [patient_creation_response.get("id")]},
        ).json()

        # scope consent

        patient_user = JheUser.objects.get(pk=patient_creation_response.get("jheUserId"))

        patient_grant = patient_user.create_authorization_code(1, settings.OIDC_CLIENT_REDIRECT_URI)

        self.remove_auth_token()

        patient_access_token, patient_refresh_token = self.get_tokens(patient_grant.code)

        self.set_auth_token(patient_access_token)

        scope_consent_response = self.session.post(  # noqa
            url=self.BASE_URL + f"/api/v1/patients/{patient_creation_response.get('id')}/consents",
            json={
                "studyScopeConsents": [
                    {
                        "studyId": study.id,
                        "scopeConsents": [
                            {
                                "codingSystem": "https://w3id.org/openmhealth",
                                "codingCode": "omh:blood-glucose:4.0",
                                "consented": True,
                            }
                        ],
                    }
                ]
            },
        )

        # FHIR Observation

        request_payload = {
            "resourceType": "Bundle",
            "type": "batch",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "status": "final",
                        "code": {
                            "coding": [{"system": "https://w3id.org/openmhealth", "code": "omh:blood-glucose:4.0"}]
                        },
                        "subject": {"reference": f"Patient/{patient_creation_response.get('id')}"},
                        "device": {"reference": "Device/70003"},
                        "identifier": [{"system": "https://ehr.example.com", "value": "bg-129-2025-01-01"}],
                        "valueAttachment": {
                            "contentType": "application/json",
                            "data": "eyJoZWFkZXIiOnsidXVpZCI6ImFhYWExMjM0LTFhMmItM2M0ZC01ZTZmLTAwMDAwMDAwMDAwMSIsInNjaG"
                            "VtYV9pZCI6eyJuYW1lIjoiYmxvb2QtZ2x1Y29zZSIsInZlcnNpb24iOiI0LjAiLCJuYW1lc3BhY2UiOiJv"
                            "bWgifSwic291cmNlX2NyZWF0aW9uX2RhdGVfdGltZSI6IjIwMjUtMDEtMDFUMDE6MDE6MDEtMDg6MDAiLC"
                            "Jtb2RhbGl0eSI6InNlbnNlZCIsImV4dGVybmFsX2RhdGFzaGVldHMiOlt7ImRhdGFzaGVldF90eXBlIjoi"
                            "bWFudWZhY3R1cmVyIiwiZGF0YXNoZWV0X3JlZmVyZW5jZSI6ImlIZWFsdGgifV19LCJib2R5Ijp7ImJsb2"
                            "9kX2dsdWNvc2UiOnsidW5pdCI6Im1nL2RMIiwidmFsdWUiOjEyOX0sImVmZmVjdGl2ZV90aW1lX2ZyYW1l"
                            "Ijp7ImRhdGVfdGltZSI6IjIwMjUtMDEtMDFUMDA6MDE6MDAtMDg6MDAifX19",
                        },
                    },
                    "request": {"method": "POST", "url": "Observation"},
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "status": "final",
                        "code": {
                            "coding": [{"system": "https://w3id.org/openmhealth", "code": "omh:blood-glucose:4.0"}]
                        },
                        "subject": {"reference": f"Patient/{patient_creation_response.get('id')}"},
                        "device": {"reference": "Device/70003"},
                        "identifier": [{"system": "https://ehr.sub.example.com", "value": "bg-128-2025-01-01"}],
                        "valueAttachment": {
                            "contentType": "application/json",
                            "data": "eyJoZWFkZXIiOnsidXVpZCI6ImFhYWExMjM0LTFhMmItM2M0ZC01ZTZmLTAwMDAwMDAwMDAwMSIsInNjaG"
                            "VtYV9pZCI6eyJuYW1lIjoiYmxvb2QtZ2x1Y29zZSIsInZlcnNpb24iOiI0LjAiLCJuYW1lc3BhY2UiOiJv"
                            "bWgifSwic291cmNlX2NyZWF0aW9uX2RhdGVfdGltZSI6IjIwMjUtMDEtMDFUMDE6MDE6MDEtMDg6MDAiL"
                            "CJtb2RhbGl0eSI6InNlbnNlZCIsImV4dGVybmFsX2RhdGFzaGVldHMiOlt7ImRhdGFzaGVldF90eXBlIjo"
                            "ibWFudWZhY3R1cmVyIiwiZGF0YXNoZWV0X3JlZmVyZW5jZSI6ImlIZWFsdGgifV19LCJib2R5Ijp7ImJsb"
                            "29kX2dsdWNvc2UiOnsidW5pdCI6Im1nL2RMIiwidmFsdWUiOjEyOX0sImVmZmVjdGl2ZV90aW1lX2ZyYW1"
                            "lIjp7ImRhdGVfdGltZSI6IjIwMjUtMDEtMDFUMDA6MDE6MDAtMDg6MDAifX19",
                        },
                    },
                    "request": {"method": "POST", "url": "Observation"},
                },
            ],
        }

        self.remove_auth_token()
        self.set_auth_token(practitioner_access_token)

        fhir_observation_response = self.session.post(url=self.BASE_URL + "/fhir/r5/", json=request_payload).json()

        print(fhir_observation_response)

        fetch_fhir_observation_response = self.session.get(
            url=self.BASE_URL + "/fhir/r5/Observation",
            params={"patient._has:Group:member:_id": study.id, "patient": patient_creation_response.get("id")},
        ).json()

        print(fetch_fhir_observation_response)

        return None
