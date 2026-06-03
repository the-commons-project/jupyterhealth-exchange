import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_remove_patient_identifier_patient_aux_data"),
    ]

    operations = [
        migrations.AlterField(
            model_name="observationidentifier",
            name="observation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="identifiers",
                to="core.observation",
            ),
        ),
    ]
