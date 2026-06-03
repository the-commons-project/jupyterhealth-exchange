from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_alter_jheclient_invitation_url_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="observation",
            old_name="aux_data",
            new_name="aux_fhir_data",
        ),
        migrations.RenameField(
            model_name="patient",
            old_name="aux_data",
            new_name="aux_fhir_data",
        ),
    ]
