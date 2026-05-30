from django.db import models


class CodeableConcept(models.Model):
    coding_system = models.CharField()
    coding_code = models.CharField()
    text = models.CharField()

    def __str__(self):
        return self.text or self.coding_code

    def as_fhir_element(self):
        # FHIR Coding shape; the data-mapping engine calls this when fanning out
        # Observation.codeable_concepts into code.coding. Empty values are pruned upstream.
        return {
            "system": self.coding_system,
            "code": self.coding_code,
            "display": self.text,
        }

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["coding_system", "coding_code"],
                name="core_codeableconcept_coding_system_coding_code",
            )
        ]
