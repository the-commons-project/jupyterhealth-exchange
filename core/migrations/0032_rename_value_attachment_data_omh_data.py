import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0031_fhirauxresource"),
    ]

    operations = [
        # Rename preserves existing OMH data; the column now only ever holds OMH payloads.
        migrations.RenameField(
            model_name="observation",
            old_name="value_attachment_data",
            new_name="omh_data",
        ),
        # omh_data and codeable_concept are null for non-OMH observations (payload in aux_fhir_data).
        migrations.AlterField(
            model_name="observation",
            name="omh_data",
            field=models.JSONField(null=True),
        ),
        migrations.AlterField(
            model_name="observation",
            name="codeable_concept",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="core.codeableconcept",
            ),
        ),
    ]
