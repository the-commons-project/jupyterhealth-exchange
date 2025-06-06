# Generated by Django 5.2 on 2025-05-10 14:56

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_alter_patient_jhe_user_practitioner_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='jheuserorganization',
            name='core_jheuserorganization_unique_jhe_user_id_organization_id',
        ),
        migrations.RemoveField(
            model_name='patient',
            name='organization',
        ),
        migrations.AddField(
            model_name='practitioner',
            name='birth_date',
            field=models.DateField(null=True),
        ),
        migrations.AddField(
            model_name='practitioner',
            name='identifier',
            field=models.CharField(null=True),
        ),
        migrations.AddField(
            model_name='practitioner',
            name='last_updated',
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name='practitioner',
            name='name_family',
            field=models.CharField(null=True),
        ),
        migrations.AddField(
            model_name='practitioner',
            name='name_given',
            field=models.CharField(null=True),
        ),
        migrations.AddField(
            model_name='practitioner',
            name='telecom_phone',
            field=models.CharField(null=True),
        ),
    ]
