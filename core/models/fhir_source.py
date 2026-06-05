from django.db import models


class FhirSource(models.Model):
    """A patient-registered upstream FHIR source.

    A patient registers a FhirSource (naming the device/data source and the upstream FHIR
    server's base URL) before uploading FHIR resources. Every FhirAuxResource row links to the
    FhirSource it came from, and the source is identified on upload by the
    ``X-JHE-FHIR-Source-ID`` request header.
    """

    patient = models.ForeignKey("Patient", on_delete=models.CASCADE, related_name="fhir_sources")
    data_source = models.ForeignKey("DataSource", on_delete=models.CASCADE, related_name="fhir_sources")
    label = models.CharField()
    fhir_base_url = models.CharField()
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.label or f"FhirSource {self.pk}"
