# A FhirSource is identified by its pk (machines) and its label (humans) -- nothing else. The
# upstream endpoint it was registered with identified neither: a source may be an EHR brand, a
# one-off import unique to one patient, or any other FHIR speaker, so `fhir_base_url` could not
# generally answer "is this the same system?" and nothing needs it to. Each source is its own
# identifier namespace (https://jupyterhealth.org/fhir/fhir-source/<pk>), which is what upstream
# record ids are scoped by, so two sources for one hospital cost nothing.
#
# The column is dropped here. Its value is folded into `label` first (when the label does not
# already name it) so deployed rows keep a human-readable trace of where they came from.

from django.db import migrations


def folded_label(label, url):
    """``label`` with ``url`` appended, or unchanged when it already names it (or there is no url)."""
    label = label or ""
    if not url or url in label:
        return label
    return f"{label} — {url}" if label else url


def fold_base_url_into_label(apps, schema_editor):
    FhirSource = apps.get_model("core", "FhirSource")
    for source in FhirSource.objects.exclude(fhir_base_url="").exclude(fhir_base_url__isnull=True).iterator():
        label = folded_label(source.label, source.fhir_base_url)
        if label != source.label:
            # .update() avoids auto_now bumping last_updated for a purely mechanical rewrite.
            FhirSource.objects.filter(pk=source.pk).update(label=label)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0046_rename_ehr_patient_portal"),
    ]

    operations = [
        migrations.RunPython(fold_base_url_into_label, migrations.RunPython.noop),
        migrations.RemoveField(model_name="fhirsource", name="fhir_base_url"),
    ]
