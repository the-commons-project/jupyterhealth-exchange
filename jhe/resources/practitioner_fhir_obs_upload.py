import argparse
import base64
import json
import os
import urllib.parse

import requests
from dotenv import load_dotenv

load_dotenv()

SITE_URL = os.getenv("SITE_URL")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID")
PATIENT_AUTHORIZATION_CODE_VERIFIER = os.getenv("PATIENT_AUTHORIZATION_CODE_VERIFIER")
OIDC_CLIENT_REDIRECT_URI = SITE_URL + os.getenv("OIDC_CLIENT_REDIRECT_URI_PATH")
PATIENT_AUTHORIZATION_CODE_CHALLENGE = os.getenv("PATIENT_AUTHORIZATION_CODE_CHALLENGE")

OMH_BLOOD_GLUCOSE_JSON = {
    "header": {
        "uuid": "aaaa1234-1a2b-3c4d-5e6f-000000000001",
        "schema_id": {"name": "blood-glucose", "version": "4.0", "namespace": "omh"},
        "source_creation_date_time": "2025-01-01T01:01:01-08:00",
        "modality": "sensed",
        "external_datasheets": [{"datasheet_type": "manufacturer", "datasheet_reference": "iHealth"}],
    },
    "body": {
        "blood_glucose": {"unit": "mg/dL", "value": 129},
        "effective_time_frame": {"date_time": "2025-01-01T00:01:00-08:00"},
    },
}


class Command:

    def __init__(self):
        self.session = requests.Session()
        self.BASE_URL = SITE_URL

    @staticmethod
    def json_to_base64_binary(payload: dict) -> str:
        """
        Convert a JSON dict/string to base64-encoded binary (UTF-8 bytes -> base64).
        This matches what your FHIR code expects in valueAttachment.data.
        """

        json_compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

        b = json_compact.encode("utf-8")
        return base64.b64encode(b).decode("ascii")

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

        self.session.post(self.BASE_URL + "/accounts/login/", allow_redirects=True, data=login_data)

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

    def handle(self, *, email: str, password: str, organization_id: int, study_id: int, patient_email: str):
        practitioner_access_token, practitioner_refresh_token = self.get_tokens(self.get_grant_code(email, password))

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

        patient_study_response = self.session.post(  # noqa
            url=self.BASE_URL + f"/api/v1/studies/{study_id}/patients",
            json={"patientIds": [patient_creation_response.get("id")]},
        ).json()

        # scope consent

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
                            "data": self.json_to_base64_binary(OMH_BLOOD_GLUCOSE_JSON),
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
                            "data": self.json_to_base64_binary(OMH_BLOOD_GLUCOSE_JSON),
                        },
                    },
                    "request": {"method": "POST", "url": "Observation"},
                },
            ],
        }

        fhir_observation_response = self.session.post(url=self.BASE_URL + "/fhir/r5/", json=request_payload).json()

        print(fhir_observation_response)

        fetch_fhir_observation_response = self.session.get(
            url=self.BASE_URL + "/fhir/r5/Observation",
            params={"patient._has:Group:member:_id": study_id, "patient": patient_creation_response.get("id")},
        ).json()

        print(fetch_fhir_observation_response)

        return None


def parse_args():
    """
    Assuming that there's already a signed-up JHE practitioner user "obs-upload@example.com" with password Jhe1234!
    Assuming there's an Organization "Obs Upload Org"
    Assuming there's a Study with "Obs Upload Study" with iHealth data source and blood glucose scope

    """
    parser = argparse.ArgumentParser(description="Obs upload script with CLI overrides.")
    parser.add_argument("--email", default="obs-upload@example.com", help="Practitioner email (default: %(default)s)")
    parser.add_argument("--password", default="Jhe1234!", help="Practitioner password (default: %(default)s)")
    parser.add_argument("--org-id", type=int, default=20003, help="Organization ID (default: %(default)s)")
    parser.add_argument("--study-id", type=int, default=30006, help="Study ID (default: %(default)s)")
    parser.add_argument(
        "--patient-email",
        default="obs-upload-pat1@example.com",
        help="Patient email to create/enroll (default: %(default)s)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    Command().handle(
        email=args.email,
        password=args.password,
        organization_id=args.org_id,
        study_id=args.study_id,
        patient_email=args.patient_email,
    )
