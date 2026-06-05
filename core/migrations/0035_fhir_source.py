"""Introduce FhirSource (a patient-registered upstream FHIR source) and link every
FhirAuxResource to one.

FhirAuxResource gains a required ``fhir_source`` FK (replacing the free-text ``source``
column) and its ``fhir_resource_id`` becomes nullable. Existing auxiliary rows predate the
required link and are disposable, so they are cleared before the non-null FK is added.
"""

import django.db.models.deletion
from django.db import migrations, models


def _clear_aux_resources(apps, schema_editor):
    apps.get_model("core", "FhirAuxResource").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_fhir_aux_overflow"),
    ]

    operations = [
        migrations.CreateModel(
            name="FhirSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("label", models.CharField()),
                ("fhir_base_url", models.CharField()),
                ("last_updated", models.DateTimeField(auto_now=True)),
                (
                    "data_source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fhir_sources",
                        to="core.datasource",
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fhir_sources",
                        to="core.patient",
                    ),
                ),
            ],
        ),
        migrations.RunPython(_clear_aux_resources, migrations.RunPython.noop),
        migrations.RemoveField(model_name="fhirauxresource", name="source"),
        migrations.AlterField(
            model_name="fhirauxresource",
            name="fhir_resource_id",
            field=models.CharField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="fhirauxresource",
            name="fhir_source",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="aux_resources",
                to="core.fhirsource",
                null=False,
            ),
        ),
    ]
