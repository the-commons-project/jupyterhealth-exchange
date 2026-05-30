from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_alter_observationidentifier_observation"),
    ]

    operations = [
        migrations.AddField(
            model_name="observation",
            name="aux_data",
            field=models.JSONField(null=True),
        ),
    ]
