import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_rename_value_attachment_data_omh_data"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="practitioner",
            name="telecom_phone",
        ),
        # auto_now fields need a value for existing rows; the one-off default (now) is applied
        # at migration time only (preserve_default=False), leaving the field as auto_now=True.
        migrations.AddField(
            model_name="study",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="datasource",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="organization",
            name="last_updated",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
