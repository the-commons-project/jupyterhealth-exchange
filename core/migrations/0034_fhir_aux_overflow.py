"""Rework FHIR overflow storage.

Removes the per-row ``aux_fhir_data`` overflow columns from Patient and Observation (FHIR
fields that don't fit a mapped model now become FhirAuxResource rows instead), and rebuilds
FhirAuxResource with a UUID primary key (so its FHIR id is disjoint from the integer pks of
the mapped models) and a required ``patient`` link. FhirAuxResource has no inbound foreign
keys, so it is dropped and recreated; any existing auxiliary rows are reset.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_practitioner_drop_phone_add_last_updated"),
    ]

    operations = [
        migrations.RemoveField(model_name="observation", name="aux_fhir_data"),
        migrations.RemoveField(model_name="patient", name="aux_fhir_data"),
        migrations.DeleteModel(name="FhirAuxResource"),
        migrations.CreateModel(
            name="FhirAuxResource",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("resource_type", models.CharField()),
                ("patient_fhir_id", models.CharField(blank=True, null=True)),
                ("fhir_resource_id", models.CharField()),
                ("fhir_data", models.JSONField(null=True)),
                ("source", models.CharField(blank=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True)),
                (
                    "patient",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="core.patient"),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["resource_type", "patient"], name="core_fhirau_resourc_59f1af_idx")],
            },
        ),
    ]
