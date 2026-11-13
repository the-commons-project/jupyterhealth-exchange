"""What a migrated database needs before JHE is usable, and nothing that belongs to any one
deployment: the reference data (settings, codeable concepts, data sources, the JHE clients, the
root organization), the OAuth application the browser login runs against, and the EHR vendors
an operator fills in. Every function creates what is missing and leaves existing rows alone,
so an operator's edits survive a rerun. `manage.py bootstrap` runs them all; `manage.py seed`,
the development fixture, runs them first and then adds its demo on top.

Each function returns the names of the rows it created, for the command's report.
"""

from __future__ import annotations

import os
import secrets
import string

from django.conf import settings
from oauth2_provider.models import get_application_model

from core.models import (
    ClientDataSource,
    CodeableConcept,
    DataSource,
    DataSourceSupportedScope,
    EhrVendor,
    JheClient,
    JheSetting,
    Organization,
)

OMH = "https://w3id.org/openmhealth"
IEEE = "https://w3id.org/ieee1752"
FHIR_RESOURCE_TYPES = "http://hl7.org/fhir/resource-types"

ADMIN_UI_APPLICATION = "JHE Admin UI"
MCP_BROKER_APPLICATION = "JHE MCP Server"
ROOT_ORGANIZATION_ID = 0


def ensure_settings() -> list[str]:
    """Every runtime setting the application reads, at a neutral value, created only when
    absent. No invite code: `site.registration_invite_code` is created empty, which keeps
    registration closed until an admin sets it."""
    defaults = [
        ("site.url", "string", settings.SITE_URL),
        ("site.ui.title", "string", "JupyterHealth Exchange"),
        ("site.ui.logo", "string", ""),
        ("site.ui.theme_css", "string", ""),
        ("site.time_zone", "string", "UTC"),
        ("site.registration_invite_code", "string", ""),
        ("auth.default_orgs", "string", ""),
        ("auth.sso.saml2", "int", 0),
        ("auth.sso.valid_domains", "string", ""),
        ("auth.patient.invitation_expiration_days", "int", 7),
        ("auth.patient.invitation_redemption_window_hours", "int", 12),
        ("auth.patient_access_clients", "json", []),
        ("auth.sof.trusted_issuers", "json", []),
        ("auth.sof.trusted_audience", "string", ""),
        ("module.ow", "bool", False),
        ("ow.sync_in_progress", "string", ""),
        ("ow.api_url", "string", ""),
        ("ow.api_key", "string", ""),
        ("ow.poll_window_days", "int", 1),
        ("ow.sleep_lookback_days", "int", 7),
    ]
    created = []
    for key, value_type, value in defaults:
        if JheSetting.objects.filter(key=key).exists():
            continue
        setting = JheSetting(key=key, value_type=value_type)
        setting.set_value(value_type, str(value) if value_type == "int" else value)
        setting.save()
        created.append(key)
    return created


CODEABLE_CONCEPTS = [
    # OMH = https://w3id.org/openmhealth, IEEE = https://w3id.org/ieee1752. IEEE 1752 is the
    # balloted form of an OMH schema and JHE treats the two coding systems interchangeably, so
    # a measure can exist under either, or both where the schema versions differ. Every label
    # names its namespace: the admin renders a concept by its text alone.
    (OMH, "omh:blood-glucose:4.0", "Blood glucose (OMH)"),
    (OMH, "omh:blood-pressure:4.0", "Blood pressure (OMH)"),
    (OMH, "omh:body-temperature:4.0", "Body temperature (OMH)"),
    (OMH, "omh:heart-rate:2.0", "Heart Rate (OMH)"),
    (OMH, "omh:oxygen-saturation:2.0", "Oxygen saturation (OMH)"),
    (OMH, "omh:respiratory-rate:2.0", "Respiratory rate (OMH)"),
    (OMH, "omh:rr-interval:1.0", "RR Interval (OMH)"),
    (OMH, "omh:body-weight:3.0", "Body weight (OMH)"),
    (OMH, "omh:body-height:2.0", "Body height (OMH)"),
    (OMH, "omh:forced-vital-capacity:1.0", "Forced vital capacity (OMH)"),
    (OMH, "omh:forced-expiratory-volume-1-second:1.0", "Forced expiratory volume 1 second (OMH)"),
    (OMH, "omh:physical-activity:1.2", "Physical activity (OMH)"),
    (OMH, "omh:step-count:3.0", "Step count (OMH)"),
    (OMH, "omh:sleep-episode:1.1", "Sleep episode (OMH)"),
    (OMH, "omh:sleep-duration:2.0", "Sleep duration (OMH)"),
    (IEEE, "ieee:sleep-stage-summary:1.0", "Sleep stage summary (IEEE)"),
    (IEEE, "ieee:physical-activity:1.0", "Physical activity (IEEE)"),
    (IEEE, "ieee:sleep-episode:1.0", "Sleep episode (IEEE)"),
    (IEEE, "ieee:total-sleep-time:1.0", "Total sleep time (IEEE)"),
    (IEEE, "ieee:time-in-bed:1.0", "Time in bed (IEEE)"),
    # Not OMH/IEEE data points: FHIR resource types, so they carry no suffix. "*" is the
    # wildcard: a data source scoped to it supplies FHIR resources of any type, which is what
    # an EHR patient-portal pull does.
    (FHIR_RESOURCE_TYPES, "QuestionnaireResponse", "FHIR QuestionnaireResponse"),
    (FHIR_RESOURCE_TYPES, "*", "All FHIR Resources"),
]


def ensure_codeable_concepts() -> list[str]:
    created = []
    for system, code, text in CODEABLE_CONCEPTS:
        _, was_created = CodeableConcept.objects.update_or_create(
            coding_system=system, coding_code=code, defaults={"text": text}
        )
        if was_created:
            created.append(code)
    return created


DATA_SOURCES = [
    ("CareX", "personal_device", ["omh:blood-pressure:4.0", "omh:heart-rate:2.0"]),
    ("Dexcom Stelo", "personal_device", ["omh:blood-glucose:4.0"]),
    ("iHealth", "personal_device", ["omh:body-temperature:4.0", "omh:heart-rate:2.0"]),
    (
        "Oura",
        "personal_device",
        [
            "omh:heart-rate:2.0",
            "omh:oxygen-saturation:2.0",
            "omh:respiratory-rate:2.0",
            "omh:body-weight:3.0",
            "omh:body-height:2.0",
            "ieee:sleep-episode:1.0",
            "ieee:sleep-stage-summary:1.0",
            "ieee:time-in-bed:1.0",
            "ieee:total-sleep-time:1.0",
            "ieee:physical-activity:1.0",
        ],
    ),
    ("Questionnaire", "patient_app", ["QuestionnaireResponse"]),
    # Clinical records pulled from an EHR's patient-access FHIR API. Shares its name with the
    # client of the same name: one product, one name. Its scope is the FHIR wildcard: the pull
    # spans every mapped resource type and lands in the auxiliary FHIR store.
    ("EHR Patient Portal", "patient_app", ["*"]),
    # Clinical records pulled server-side and delivered to the deployment's bucket.
    ("USCDI Patient Access", "patient_app", ["*"]),
]


def ensure_data_sources() -> list[str]:
    """The names and types are the code's: a retype updates the row in place (matching on
    (name, type) would create a second row and orphan the rows pointing at the first). The
    labels of codeable concepts are the code's too; everything else here is create-only."""
    created = []
    for name, source_type, scope_codes in DATA_SOURCES:
        source, was_created = DataSource.objects.update_or_create(name=name, defaults={"type": source_type})
        if was_created:
            created.append(name)
        for coding_code in scope_codes:
            scope = CodeableConcept.objects.get(coding_code=coding_code)
            DataSourceSupportedScope.objects.get_or_create(data_source=source, scope_code=scope)
    return created


def _client_id(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def clients() -> list[dict]:
    """The JHE clients: a patient-facing application, the data sources it supplies, and the URL
    an invitation to it opens. The three JHE serves itself live under SITE_URL; the two
    external products have their own hosts."""
    site = settings.SITE_URL
    return [
        {
            "name": "CareX",
            "invitation_url": "https://carex.ai/invitation/CODE",
            "data_sources": ["CareX", "Questionnaire"],
        },
        {
            "name": "CommonHealth",
            "invitation_url": "https://commonhealth.tcp.org?invitation=CODE",
            "data_sources": ["Dexcom Stelo", "iHealth"],
        },
        {
            # The Open Wearables patient client, served by JHE at /clients/ow/launch.
            "name": "Open Wearables",
            "invitation_url": f"{site}/clients/ow/launch?code=CODE",
            "data_sources": ["Oura"],
        },
        {
            # The browser-side EHR records client. Its application's redirect_uris is JHE's own
            # OAuth callback (the invitation -> /o/token exchange), not the EHR callback, which
            # is registered on the EHR app as `{site}/clients/ehr-patient-portal/callback`.
            "name": "EHR Patient Portal",
            "invitation_url": f"{site}/clients/ehr-patient-portal/?code=CODE",
            "data_sources": ["EHR Patient Portal"],
        },
        {
            # The server-side counterpart: the shell redeems the invitation for a token and
            # the EHR returns to /clients/uscdi-patient-access/callback.
            "name": "USCDI Patient Access",
            "invitation_url": f"{site}/clients/uscdi-patient-access/?code=CODE",
            "data_sources": ["USCDI Patient Access"],
        },
    ]


def ensure_clients() -> list[str]:
    """The client applications and their JheClient rows. The invitation URL is written once,
    when the client is created: a deployment's real host differs from any default, so an
    operator's edit is never overwritten."""
    application_model = get_application_model()
    created = []
    for client in clients():
        app, was_created = application_model.objects.get_or_create(
            name=client["name"],
            defaults={
                "client_id": _client_id(),
                "client_type": application_model.CLIENT_PUBLIC,
                "authorization_grant_type": application_model.GRANT_AUTHORIZATION_CODE,
                "skip_authorization": True,
                "redirect_uris": settings.SITE_URL + settings.OAUTH2_CALLBACK_PATH,
                "algorithm": "RS256",
            },
        )
        jhe_client, _ = JheClient.objects.get_or_create(application=app)
        if was_created:
            jhe_client.invitation_url = client["invitation_url"]
            jhe_client.save()
            created.append(client["name"])
        for source_name in client["data_sources"]:
            source = DataSource.objects.get(name=source_name)
            ClientDataSource.objects.get_or_create(client=app, data_source=source)
    return created


def ensure_root_organization() -> list[str]:
    _, was_created = Organization.objects.get_or_create(
        id=ROOT_ORGANIZATION_ID, defaults={"name": "ROOT", "type": "root"}
    )
    return ["ROOT"] if was_created else []


def ensure_admin_ui_application() -> list[str]:
    """The OAuth application the browser login runs against: the context processor reads its
    client id by this name, and without it nobody can log in. A public client with PKCE, no
    secret, consent skipped (it is the site's own UI), redirecting to JHE's callback."""
    application_model = get_application_model()
    # `filter().exists()` rather than get_or_create: a database that was seeded before this
    # command existed may hold two rows of this name (one by hand, one from the seed), and the
    # lookup takes the first by id, so both work. Nothing to create then, and nothing to pick.
    if application_model.objects.filter(name=ADMIN_UI_APPLICATION).exists():
        return []
    application_model.objects.create(
        name=ADMIN_UI_APPLICATION,
        client_type=application_model.CLIENT_PUBLIC,
        authorization_grant_type=application_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris=settings.SITE_URL + settings.OAUTH2_CALLBACK_PATH,
        client_secret="",
        skip_authorization=True,
        algorithm="RS256",
        user_id=None,
    )
    return [ADMIN_UI_APPLICATION]


EHR_VENDORS = ["Epic Sandbox", "Epic Production"]


def ensure_ehr_vendors() -> list[str]:
    """The vendor rows the patient-portal connect flow is configured on, with no credentials:
    the client id is the deployment's own EHR app registration, entered on the EHRs page.
    Brands and locations come from `import_ehr_brands` with the vendor's published bundle."""
    created = []
    for name in EHR_VENDORS:
        _, was_created = EhrVendor.objects.get_or_create(
            name=name, defaults={"supported_scopes": " ".join(EhrVendor.SUPPORTED_SCOPES)}
        )
        if was_created:
            created.append(name)
    return created


def ensure_mcp_broker_application() -> list[str] | None:
    """The MCP server's confidential client, from MCP_OAUTH_CLIENT_ID and MCP_OAUTH_CLIENT_SECRET
    (the broker reads the same pair as JHE_CLIENT_ID / JHE_CLIENT_SECRET), so no secret is in
    the code. None when they are not set: nothing to do. Unlike the rest, an existing row is
    updated, because the credentials are the deployment's and this is how they are rotated."""
    client_id = os.environ.get("MCP_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("MCP_OAUTH_CLIENT_SECRET")
    if not (client_id and client_secret):
        return None
    redirect_uri = os.environ.get("MCP_OAUTH_REDIRECT_URI", "https://jhe-mcp.fly.dev/oauth/callback")
    _, was_created = get_application_model().objects.update_or_create(
        name=MCP_BROKER_APPLICATION,
        defaults={
            "client_id": client_id,
            "client_secret": client_secret,  # hashed on save (hash_client_secret)
            "hash_client_secret": True,
            "client_type": "confidential",
            "authorization_grant_type": "authorization-code",
            "redirect_uris": redirect_uri,
            "skip_authorization": True,
            "algorithm": "RS256",
            "user_id": None,
        },
    )
    return [MCP_BROKER_APPLICATION] if was_created else []
