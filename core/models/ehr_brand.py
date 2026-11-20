from django.core.exceptions import ValidationError
from django.db import models

from patient_access.core.vendors import check_query_override
from patient_access.core.vocab import ScopeVersion


def query_override_errors(value) -> list[str]:
    """What is wrong with an `EhrVendor.query_overrides` value, for the Django admin and the API.
    Empty when it is a list of {"row": <query-table row label>, "query": <search>} objects, at most
    one per row, each search one `check_query_override` accepts."""
    if not isinstance(value, list):
        return ["Query overrides must be a list of {row, query} objects."]
    errors: list[str] = []
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"row", "query"}
            or not all(isinstance(v, str) for v in item.values())
        ):
            errors.append("Each query override must be a {row, query} object of two strings.")
            continue
        if item["row"] in seen:
            errors.append(f"{item['row']} has more than one override.")
            continue
        seen.add(item["row"])
        try:
            check_query_override(item["row"], item["query"])
        except ValueError as e:
            errors.append(str(e))
    return errors


class EhrVendor(models.Model):
    """
    An EHR vendor (e.g. Epic). JHE's OAuth client registration and supported scopes are
    per-vendor, not per-brand: a single vendor app registration (e.g. Epic App Orchard)
    works across every brand (hospital/health system) running that vendor's EHR, discovered
    per-brand via the SMART `iss` -- see EhrBrand.
    """

    # Non-resource plumbing scopes every connection needs regardless of which data types the
    # patient agrees to share; always requested in addition to whichever of SUPPORTED_SCOPES
    # the patient picks in the "choose what to share" step.
    BASE_SCOPES = "openid profile launch/patient"

    # patient/<resource>.read scope -> friendly label, for the "choose what to
    # share" checklist (EhrVendor.supported_scopes is a space-delimited subset
    # of these keys). Keep in sync with EHR_PATIENT_PORTAL_PULLS in
    # client-ehr-patient-portal.js, which drives the actual per-type pulls.
    SUPPORTED_SCOPES = {
        "patient/Patient.read": "Demographics",
        "patient/Condition.read": "Conditions",
        "patient/MedicationRequest.read": "Medications",
        "patient/MedicationDispense.read": "Medication Dispenses",
        "patient/AllergyIntolerance.read": "Allergies",
        "patient/Immunization.read": "Immunizations",
        "patient/Procedure.read": "Procedures",
        "patient/Observation.read": "Labs & Vital Signs",
        "patient/DiagnosticReport.read": "Diagnostic Reports",
        "patient/DocumentReference.read": "Documents",
        "patient/Encounter.read": "Encounters",
        "patient/CarePlan.read": "Care Plans",
        "patient/CareTeam.read": "Care Teams",
        "patient/Goal.read": "Goals",
        "patient/ServiceRequest.read": "Service Requests",
        "patient/Device.read": "Devices",
        "patient/QuestionnaireResponse.read": "Questionnaire Responses",
    }

    # The vendor row names code refers to. The only place runtime code spells them; migrations
    # 0053 and 0054 repeat them, as migrations must.
    EPIC_SANDBOX = "Epic Sandbox"
    EPIC_PRODUCTION = "Epic Production"
    ORACLE_HEALTH_SANDBOX = "Oracle Health Sandbox"
    ORACLE_HEALTH_PRODUCTION = "Oracle Health Production"

    # What USCDI Patient Access needs set on an Epic row: Epic's own authorization screen asks the
    # patient which record types to share and ignores a narrower request, and `profile` is the
    # sign-in scope JHE has always sent Epic.
    EPIC_DEFAULTS = {"skip_type_checklist": True, "extra_scopes": "profile"}
    # What it needs set on an Oracle Health (Cerner Millennium) row: Millennium serves a document's
    # content only to a token that also carries patient/Binary.read.
    ORACLE_HEALTH_DEFAULTS = {"extra_scopes": "patient/Binary.read"}
    # The settings a row gets when bootstrap or import_ehr_brands creates it, by the row's name;
    # migrations 0053 and 0054 set the same on rows that already existed. Defaults only: an
    # operator's edits on the EHRs admin page always win. This is for a deviation every deployment
    # of that vendor shares; anything particular to one deployment is set on its row instead, so
    # keep this list short. A row under any other name starts with none.
    DEFAULTS_BY_NAME = {
        EPIC_SANDBOX: EPIC_DEFAULTS,
        EPIC_PRODUCTION: EPIC_DEFAULTS,
        ORACLE_HEALTH_SANDBOX: ORACLE_HEALTH_DEFAULTS,
        ORACLE_HEALTH_PRODUCTION: ORACLE_HEALTH_DEFAULTS,
    }

    @classmethod
    def defaults_for(cls, name: str) -> dict:
        """The sign-in settings a new row named `name` starts with (see DEFAULTS_BY_NAME)."""
        return dict(cls.DEFAULTS_BY_NAME.get(name, {}))

    name = models.CharField(unique=True)
    ehr_client_id = models.CharField(null=True, blank=True)
    supported_scopes = models.TextField(null=True, blank=True)
    # USCDI Patient Access only (the EHR Patient Portal does not read these).
    skip_type_checklist = models.BooleanField(
        default=False,
        help_text="The EHR's own authorization screen asks the patient which record types to share, so JHE "
        "skips its checklist and requests every supported scope (Epic).",
    )
    extra_scopes = models.TextField(
        blank=True,
        default="",
        help_text="Space-separated scopes requested in addition to openid fhirUser launch/patient, for an "
        "EHR whose sign-in or records need more (Epic: profile).",
    )
    scope_version = models.CharField(
        max_length=2,
        choices=[(ScopeVersion.V1.value, "v1 (patient/X.read)"), (ScopeVersion.V2.value, "v2 (patient/X.rs)")],
        default=ScopeVersion.V1.value,
        help_text="The SMART App Launch version the EHR's scopes are spelled in.",
    )
    query_overrides = models.JSONField(
        blank=True,
        default=list,
        help_text="Replacement searches for rows of the query table, for an EHR that answers a row "
        'differently: a list of {"row": <row label>, "query": <search relative to the FHIR base>}.',
    )
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or f"EhrVendor {self.pk}"

    def clean(self):
        super().clean()
        if errors := query_override_errors(self.query_overrides):
            raise ValidationError({"query_overrides": errors})

    def query_override_map(self) -> dict[str, str]:
        """`query_overrides` as row label -> search, what the library takes."""
        return {o["row"]: o["query"] for o in self.query_overrides or ()}


class EhrBrand(models.Model):
    """
    A patient-recognizable EHR organization/brand (SMART App Launch 2.2
    "user-access brand"). fhir_base_url is the SMART `iss` the patient is sent to; the
    authorize/token endpoints are discovered from it via
    `{fhir_base_url}/.well-known/smart-configuration`. The OAuth client id and supported
    scopes used to connect live on `vendor`, not here -- see EhrVendor.
    """

    name = models.CharField()
    vendor = models.ForeignKey(EhrVendor, on_delete=models.PROTECT, related_name="brands")
    fhir_base_url = models.TextField(unique=True)
    fhir_version = models.CharField(null=True, blank=True)
    npi_type_2 = models.CharField(null=True, blank=True)
    logo_url = models.TextField(null=True, blank=True)  # reserved; Epic brands file has no logo
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or f"EhrBrand {self.pk}"


class EhrBrandLocation(models.Model):
    """
    A physical care facility under a brand -- the row a patient searches and
    picks. All locations of a brand share the brand's fhir_base_url.
    """

    brand = models.ForeignKey(EhrBrand, on_delete=models.CASCADE, related_name="locations")
    name = models.CharField()
    address_text = models.TextField(blank=True, default="")
    city = models.CharField(null=True, blank=True)
    state = models.CharField(null=True, blank=True)
    postal_code = models.CharField(null=True, blank=True)
    country = models.CharField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["state"]),
            models.Index(fields=["postal_code"]),
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return self.name or f"EhrBrandLocation {self.pk}"
