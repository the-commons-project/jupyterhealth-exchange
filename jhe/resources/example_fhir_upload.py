import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jhe.settings")

import django

django.setup()

import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from random import SystemRandom

import requests
from dotenv import load_dotenv
from oauth2_provider.models import get_grant_model

load_dotenv()

SITE_URL = os.getenv("SITE_URL")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID")
PATIENT_AUTHORIZATION_CODE_VERIFIER = os.getenv("PATIENT_AUTHORIZATION_CODE_VERIFIER")
OIDC_CLIENT_REDIRECT_URI = SITE_URL + os.getenv("OIDC_CLIENT_REDIRECT_URI_PATH")
PATIENT_AUTHORIZATION_CODE_CHALLENGE = os.getenv("PATIENT_AUTHORIZATION_CODE_CHALLENGE")


class Command:

    def __init__(self):
        self.session = requests.Session()
        self.BASE_URL = SITE_URL

    def get_grant_code(self, email: str, password: str):
        r = self.session.get(self.BASE_URL + "/accounts/login/", allow_redirects=True)

        csrf_token = (
            r.text.split("csrfmiddlewaretoken")[1].split("\n")[0].split('="')[1].replace('"', "").replace(">", "")
        )

        login_data = {
            "username": email,
            "password": password,
            "csrfmiddlewaretoken": csrf_token,
        }

        r2 = self.session.post(self.BASE_URL + "/accounts/login/", allow_redirects=True, data=login_data)

        authorize_params = {
            "client_id": OIDC_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.BASE_URL + "/auth/callback",
            "scope": "openid",
            "code_challenge": PATIENT_AUTHORIZATION_CODE_CHALLENGE,
            "code_challenge_method": "S256",
        }

        r3 = self.session.get(SITE_URL + "/o/authorize/", params=authorize_params, allow_redirects=True)
        parsed = urllib.parse.urlparse(r3.url)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs["code"][-1]
        return code

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
                "client_id": OIDC_CLIENT_ID,
                "code_verifier": PATIENT_AUTHORIZATION_CODE_VERIFIER,
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

        practitioner_access_token, practitioner_refresh_token = self.get_tokens(self.get_grant_code(email, password))

        organization_id = 20003

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
                "organizationId": organization_id,
                "identifier": "23455",
                "nameFamily": "jake",
                "nameGiven": "doe",
                "birthDate": "2024-11-02",
                "telecomEmail": patient_email,
                "telecomPhone": "+1-202-555-0342",
            },
        ).json()

        # Add patient to study

        # study = Study.objects.filter(name=study_name).first()
        study_id = 30006

        patient_study_response = self.session.post(  # noqa
            url=self.BASE_URL + f"/api/v1/studies/{study_id}/patients",
            json={"patientIds": [patient_creation_response.get("id")]},
        ).json()

        # scope consent

        Grant = get_grant_model()

        Grant.objects.filter(user_id=patient_creation_response["jheUserId"]).delete()

        UNICODE_ASCII_CHARACTER_SET = "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789"
        authorization_code = "".join(SystemRandom().choice(UNICODE_ASCII_CHARACTER_SET) for _ in range(30))
        patient_grant = Grant.objects.create(
            application_id=1,
            user_id=patient_creation_response["jheUserId"],
            code=authorization_code,
            expires=datetime.now(timezone.utc) + timedelta(seconds=1209600),
            redirect_uri=OIDC_CLIENT_REDIRECT_URI,
            scope="openid",
            code_challenge=PATIENT_AUTHORIZATION_CODE_CHALLENGE,
            code_challenge_method="S256",
            nonce="",
            claims=json.dumps({}),
        )

        self.remove_auth_token()

        patient_access_token, patient_refresh_token = self.get_tokens(patient_grant.code)

        self.set_auth_token(patient_access_token)

        scope_consent_response = self.session.post(  # noqa
            url=self.BASE_URL + f"/api/v1/patients/{patient_creation_response.get('id')}/consents",
            json={
                "studyScopeConsents": [
                    {
                        "studyId": study_id,
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
            params={"patient._has:Group:member:_id": study_id, "patient": patient_creation_response.get("id")},
        ).json()

        print(fetch_fhir_observation_response)

        return None


if __name__ == "__main__":
    command = Command()

    command.handle()
