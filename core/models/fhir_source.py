from django.db import models


class FhirSource(models.Model):
    """A patient-registered upstream FHIR source.

    A patient registers a FhirSource (naming the device/data source it stands for) before
    uploading FHIR resources. Every FhirAuxResource row links to the FhirSource it came
    through, and the source is identified on upload by the ``X-JHE-FHIR-Source-ID`` header.

    A source is identified by its **pk** for machines and its **label** for humans -- nothing
    else. It carries no upstream endpoint: a source may be an EHR the patient connected, a
    one-off import unique to that patient, or anything else that speaks FHIR, so there is no
    field that could identify "the same system" in general. Registering the same upstream twice
    simply makes two sources, which is cheap and harmless -- each is its own identifier
    namespace (``fhir_source_uri``), and upstream record ids are only ever unique *within* one.
    """

    patient = models.ForeignKey("Patient", on_delete=models.CASCADE, related_name="fhir_sources")
    data_source = models.ForeignKey("DataSource", on_delete=models.CASCADE, related_name="fhir_sources")
    label = models.CharField(blank=True, default="")
    # The facility the patient picked in the hospital picker, when they picked one at all --
    # descriptive only, and null for a source that is not a supported EHR at a supported location.
    #
    # It records the patient's *selection*, NOT where the data technically came from: every
    # location of a brand shares one fhir_base_url, so the connection cannot tell facilities
    # apart. Do not read it as provenance or filter clinical records on it.
    #
    # SET_NULL because deleting a brand cascades to its locations, and losing this descriptive
    # link must never take a patient's source (and every aux row under it) with it.
    ehr_brand_location = models.ForeignKey(
        "EhrBrandLocation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fhir_sources",
    )
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.label or f"FhirSource {self.pk}"
