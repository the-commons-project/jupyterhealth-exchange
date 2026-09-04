import logging

from django.db.models import Prefetch, Q
from django.shortcuts import render
from oauth2_provider.models import get_application_model
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import ClientDataSource, EhrBrandLocation, PatientIdentifier

logger = logging.getLogger(__name__)
Application = get_application_model()

# The seeded Application row this client is served from. Its DataSource of the same name is
# NOT looked up by name here -- see _config_context.
EHR_PATIENT_PORTAL_CLIENT_NAME = "EHR Patient Portal"
BRANDS_DEFAULT_LIMIT = 25
BRANDS_MAX_LIMIT = 100

# Full state/territory name (lowercase) -> USPS abbreviation, so the free-text `q` search in
# brands_search can match either ("Wisconsin" or "WI") against EhrBrandLocation.state.
US_STATES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def _ehr_patient_portal_client():
    """The seeded EHR Patient Portal Application, with its JheClient and data-source links."""
    return (
        Application.objects.filter(name=EHR_PATIENT_PORTAL_CLIENT_NAME)
        .select_related("jhe_client")
        .prefetch_related(Prefetch("data_sources", queryset=ClientDataSource.objects.order_by("id")))
        .first()
    )


def _config_context():
    app = _ehr_patient_portal_client()
    jhe_client = getattr(app, "jhe_client", None) if app else None
    aux = (jhe_client.aux_data if jhe_client else None) or {}
    # The FhirSource the browser registers needs a DataSource id. It is read off the client's
    # ClientDataSource link -- established by seed or by an admin -- so the association is
    # declared data, not something this view infers by matching on a name at request time.
    # The portal client has exactly one link; ordering by id keeps the choice stable rather
    # than arbitrary if an admin ever adds a second.
    link = next(iter(app.data_sources.all()), None) if app else None
    return {
        "ehr_patient_portal_client_id": aux.get("client_id", ""),
        "ehr_patient_portal_scopes": aux.get("scopes", ""),
        "ehr_patient_portal_data_source_id": link.data_source_id if link else "",
        # Brand values are client-specific, so they live in aux_data (§7.6), with JH defaults.
        # The default is the full lockup (icon + wordmark); the header shows it alone, with no
        # adjacent brand-name text, since the wordmark already carries the name. A rebrand's
        # own mark is not a lockup, so it renders alongside the name instead (brand_name_visible).
        "brand_name": aux.get("brand_name", "JupyterHealth"),
        "brand_logo": aux.get("brand_logo", "common/images/jupyterhealth-logo.png"),
        "brand_name_visible": "brand_logo" in aux,
    }


def ehr_patient_portal_connect(request):
    """Patient-facing start page: invitation -> JHE token -> Epic authorize."""
    return render(request, "clients/ehr-patient-portal/connect.html", _config_context())


def ehr_patient_portal_callback(request):
    """Return page: FHIR.oauth2.ready() -> store id -> pull Labs -> write to JHE."""
    return render(request, "clients/ehr-patient-portal/callback.html", _config_context())


def _parse_limit(raw):
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return BRANDS_DEFAULT_LIMIT
    return max(1, min(limit, BRANDS_MAX_LIMIT))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def brands_search(request):
    """
    GET /api/v1/ehr-patient-portal/brands?q=&state=&postal=&limit=
    Search hospital facilities for the EHR Patient Portal picker. `q` matches facility name,
    city, or brand name; `state`/`postal` filter those columns. Each result carries
    the brand's fhir_base_url (the SMART `iss`) the browser authorizes against.
    """
    qs = EhrBrandLocation.objects.select_related("brand")

    q = (request.query_params.get("q") or "").strip()
    if q:
        # Also match state by full name or abbreviation and by ZIP prefix, so "WI", "Wisconsin"
        # and "53593" all find a facility, not just its name/city/brand.
        state_filter = Q(state__iexact=q)
        if q.lower() in US_STATES:
            state_filter |= Q(state__iexact=US_STATES[q.lower()])
        qs = qs.filter(
            Q(name__icontains=q) | Q(city__icontains=q) | Q(brand__name__icontains=q) | state_filter
            | Q(postal_code__startswith=q)
        )
    state = (request.query_params.get("state") or "").strip()
    if state:
        qs = qs.filter(state__iexact=state)
    postal = (request.query_params.get("postal") or "").strip()
    if postal:
        qs = qs.filter(postal_code__istartswith=postal)

    limit = _parse_limit(request.query_params.get("limit"))
    qs = qs.order_by("brand__name", "name")[:limit]

    results = [
        {
            # The id is what the client sends back as FhirSource.ehr_brand_location, recording
            # which facility the patient picked (the connection itself cannot tell them apart).
            "id": loc.id,
            "facility_name": loc.name,
            "address_text": loc.address_text,
            "city": loc.city,
            "state": loc.state,
            "postal_code": loc.postal_code,
            "brand_name": loc.brand.name,
            "fhir_base_url": loc.brand.fhir_base_url,
        }
        for loc in qs
    ]
    return Response({"results": results})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def save_patient_identifier(request):
    """
    POST /api/v1/ehr-patient-portal/identifier  {system, value}
    Additively attach an external identifier (the EHR patient id) to the
    authenticated patient. get_or_create keeps it idempotent and never replaces
    the patient's other identifiers (unlike the practitioner PATCH path).
    """
    patient = request.user.get_patient()
    if patient is None:
        return Response({"error": "Authenticated user is not a patient"}, status=400)

    system = request.data.get("system")
    value = request.data.get("value")
    if not system or not value:
        return Response({"error": "system and value are required"}, status=400)

    identifier, created = PatientIdentifier.objects.get_or_create(
        system=system, value=value, defaults={"patient": patient}
    )
    # (system, value) is globally unique, so the existing row may belong to someone else.
    # Reporting 200 there would tell the caller it was attached when it was not.
    if not created and identifier.patient_id != patient.id:
        return Response({"error": "Identifier is already assigned to another patient"}, status=409)
    return Response({"system": system, "value": value})
