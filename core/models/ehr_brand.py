from django.db import models


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

    name = models.CharField(unique=True)
    ehr_client_id = models.CharField(null=True, blank=True)
    supported_scopes = models.TextField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or f"EhrVendor {self.pk}"


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
