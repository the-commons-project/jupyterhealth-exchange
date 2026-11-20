# Moves the last two per-vendor settings of USCDI Patient Access from the library's code registry onto
# the EhrVendor row: the SMART scope version (v1 for every vendor until now) and replacement searches
# for rows of the query table. The registry's one replacement was for the dev seed's "SMART Sandbox"
# row (the SMART reference server leaves DocumentReference.category empty, so the clinical-note
# filter hid every note); that row gets it here, unless an operator already set overrides on it. The
# reverse removes the fields.

from django.db import migrations, models

SMART_SANDBOX = "SMART Sandbox"  # seed_patient_access_sandbox's vendor row
SMART_SANDBOX_OVERRIDES = [{"row": "Documents", "query": "DocumentReference"}]


def set_smart_sandbox_overrides(apps, schema_editor):
    EhrVendor = apps.get_model("core", "EhrVendor")
    EhrVendor.objects.filter(name=SMART_SANDBOX, query_overrides=[]).update(query_overrides=SMART_SANDBOX_OVERRIDES)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0054_ehrvendor_oracle_health_binary_read"),
    ]

    operations = [
        migrations.AddField(
            model_name="ehrvendor",
            name="query_overrides",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Replacement searches for rows of the query table, for an EHR that answers a row "
                'differently: a list of {"row": <row label>, "query": <search relative to the FHIR base>}.',
            ),
        ),
        migrations.AddField(
            model_name="ehrvendor",
            name="scope_version",
            field=models.CharField(
                choices=[("v1", "v1 (patient/X.read)"), ("v2", "v2 (patient/X.rs)")],
                default="v1",
                help_text="The SMART App Launch version the EHR's scopes are spelled in.",
                max_length=2,
            ),
        ),
        migrations.RunPython(set_smart_sandbox_overrides, migrations.RunPython.noop),
    ]
