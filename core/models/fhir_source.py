from django.db import models


class FhirSourceManager(models.Manager):
    def get_or_create_for_pull(self, patient, data_source, location):
        """The FhirSource a server-side pull (USCDI Patient Access) is recorded under: the patient's
        existing source for the picked facility's brand and this data source, else a new one --
        the one-per-brand rule FhirSourceViewSet.create applies, so a reconnect reuses its source
        and repeat pulls are told apart by their own timestamps. Returns ``(source, created)``."""
        brand = location.brand if location is not None else None
        if brand is not None:
            existing = (
                self.filter(patient=patient, data_source=data_source, ehr_brand_location__brand=brand)
                .order_by("id")
                .first()
            )
            if existing is not None:
                return existing, False
        label = " - ".join(filter(None, [brand.vendor.name, brand.name])) if brand is not None else "EHR records"
        return self.create(patient=patient, data_source=data_source, label=label, ehr_brand_location=location), True


class FhirSource(models.Model):
    """A patient-registered upstream FHIR source.

    A patient registers a FhirSource (naming the device/data source it stands for) before
    uploading FHIR resources. Every FhirAuxResource row links to the FhirSource it came
    through, and the source is identified on upload by the ``X-JHE-FHIR-Source-ID`` header.

    A source is identified by its **pk** for machines and its **label** for humans -- nothing
    else. It carries no upstream endpoint: a source may be an EHR the patient connected, a
    one-off import unique to that patient, or anything else that speaks FHIR, so there is no
    field that could identify "the same system" in general. Each source is its own identifier
    namespace (``fhir_source_uri``), and upstream record ids are only ever unique *within* one.

    One thing does identify "the same system" for an EHR connection: the brand behind
    ``ehr_brand_location``. FhirSourceViewSet.create uses it to hand a patient back their existing
    source for a brand instead of registering a second, because a second source would store every
    record again under its own namespace. A source with no brand (a one-off import, or a launch
    whose brand cannot be resolved) is never matched and always creates.
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

    objects = FhirSourceManager()

    def __str__(self):
        return self.label or f"FhirSource {self.pk}"
