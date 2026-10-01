# A FhirSource is identified by its pk (machines) and its label (humans) -- nothing else. The
# upstream endpoint it was registered with identified neither: a source may be an EHR brand, a
# one-off import unique to one patient, or any other FHIR speaker, so `fhir_base_url` could not
# generally answer "is this the same system?" and nothing needs it to. Each source is its own
# identifier namespace (https://jupyterhealth.org/fhir/fhir-source/<pk>), which is what upstream
# record ids are scoped by.
#
# The column is dropped here. The label is patient-facing, so the URL is not copied into it.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0046_rename_ehr_patient_portal"),
    ]

    operations = [
        migrations.RemoveField(model_name="fhirsource", name="fhir_base_url"),
    ]
