# Two unrelated-in-effect but same-shape changes to the EHR brand tables and FhirSource.
#
# 1. Field consistency with the rest of core.models: every other model records exactly
#    `last_updated = DateTimeField(auto_now=True)` and no model carries a creation timestamp, so
#    EhrBrand's `updated_at` is renamed and its `created_at` dropped (nothing read either), and
#    EhrBrandLocation -- which had no timestamp at all -- gains `last_updated`. `name` becomes a
#    CharField on both, matching Study.name / Organization.name / DataSource.name (identical in
#    Postgres; the visible difference is that the admin renders TextField as a textarea).
#
# 2. FhirSource gains a nullable reference to the EhrBrandLocation the patient picked, so the
#    facility chosen in the hospital picker is recorded instead of discarded. Descriptive only.

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0047_drop_fhir_source_base_url"),
    ]

    operations = [
        migrations.RenameField(model_name="ehrbrand", old_name="updated_at", new_name="last_updated"),
        migrations.RemoveField(model_name="ehrbrand", name="created_at"),
        migrations.AlterField(model_name="ehrbrand", name="name", field=models.CharField()),
        migrations.AlterField(model_name="ehrbrandlocation", name="name", field=models.CharField()),
        migrations.AddField(
            model_name="ehrbrandlocation",
            name="last_updated",
            # Existing rows have no history to draw on; auto_now takes over from the next save.
            field=models.DateTimeField(auto_now=True, default=timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="fhirsource",
            name="ehr_brand_location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fhir_sources",
                to="core.ehrbrandlocation",
            ),
        ),
    ]
