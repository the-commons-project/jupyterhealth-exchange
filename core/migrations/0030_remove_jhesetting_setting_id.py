from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0029_rename_aux_data_to_aux_fhir_data"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="jhesetting",
            name="core_jhesetting_unique_key_setting_id",
        ),
        migrations.RemoveField(
            model_name="jhesetting",
            name="setting_id",
        ),
        migrations.AlterField(
            model_name="jhesetting",
            name="key",
            field=models.CharField(unique=True),
        ),
    ]
