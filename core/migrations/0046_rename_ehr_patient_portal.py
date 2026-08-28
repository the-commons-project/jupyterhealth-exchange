# Rename the seeded EHR patient-portal client and its DataSource to "EHR Patient Portal",
# and link the two.
#
# The Application row was named "Patient Access" (#671), colliding with the older, unrelated
# `auth.patient_access_clients` JheSetting (#604) -- a list of OAuth client_ids routed to the
# email one-time-code login. One is a login mode, the other a SMART client that pulls EHR
# records; sharing a name invited exactly the wrong action (adding this client's id to that
# setting). Its DataSource carried a third spelling, "Patient Access API".
#
# The client and the data source are one product, so they take one name, as CareX does. The
# rows are renamed in place -- FhirSource.data_source, StudyDataSource and ClientDataSource
# all reference them by FK, so a rename is transparent while creating fresh rows would
# orphan existing data. The DataSource is also retyped: an EHR patient portal is not a
# "medical_device".
#
# The client's URL path moves to /clients/ehr-patient-portal/ in the same change. That path
# is registered as a redirect URI on the Epic app, so the Epic-side registration must be
# updated in step or the OAuth redirect will be rejected. JHE's own api/v1 endpoints move
# to /api/v1/ehr-patient-portal/ at the same time; nothing upstream knows about those.

from django.conf import settings
from django.db import migrations

OLD_CLIENT_NAME = "Patient Access"
NEW_CLIENT_NAME = "EHR Patient Portal"
OLD_DATA_SOURCE_NAME = "Patient Access API"
NEW_DATA_SOURCE_NAME = "EHR Patient Portal"
OLD_DATA_SOURCE_TYPE = "medical_device"
NEW_DATA_SOURCE_TYPE = "patient_app"


def _rename(model, old_name, new_name, **extra):
    # Application.name and DataSource.name carry no unique constraint, so a row under the new
    # name may already exist (a database seeded after the rename shipped). Renaming then would
    # produce two rows a name lookup could not choose between; there is nothing to do instead.
    if model.objects.filter(name=new_name).exists():
        return 0
    return model.objects.filter(name=old_name).update(name=new_name, **extra)


def rename_to_ehr_patient_portal(apps, schema_editor):
    Application = apps.get_model(settings.OAUTH2_PROVIDER_APPLICATION_MODEL)
    DataSource = apps.get_model("core", "DataSource")
    ClientDataSource = apps.get_model("core", "ClientDataSource")

    _rename(Application, OLD_CLIENT_NAME, NEW_CLIENT_NAME)
    _rename(DataSource, OLD_DATA_SOURCE_NAME, NEW_DATA_SOURCE_NAME, type=NEW_DATA_SOURCE_TYPE)

    # Link them. The connect page reads the data source id off this row; before it existed the
    # view matched a hardcoded name at request time, which is why the two names could drift.
    app = Application.objects.filter(name=NEW_CLIENT_NAME).first()
    data_source = DataSource.objects.filter(name=NEW_DATA_SOURCE_NAME).first()
    if app and data_source:
        ClientDataSource.objects.get_or_create(client=app, data_source=data_source)


def restore_patient_access_names(apps, schema_editor):
    Application = apps.get_model(settings.OAUTH2_PROVIDER_APPLICATION_MODEL)
    DataSource = apps.get_model("core", "DataSource")
    ClientDataSource = apps.get_model("core", "ClientDataSource")

    app = Application.objects.filter(name=NEW_CLIENT_NAME).first()
    data_source = DataSource.objects.filter(name=NEW_DATA_SOURCE_NAME).first()
    if app and data_source:
        ClientDataSource.objects.filter(client=app, data_source=data_source).delete()

    _rename(DataSource, NEW_DATA_SOURCE_NAME, OLD_DATA_SOURCE_NAME, type=OLD_DATA_SOURCE_TYPE)
    _rename(Application, NEW_CLIENT_NAME, OLD_CLIENT_NAME)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0045_remove_idp_metadata_url_setting"),
        migrations.swappable_dependency(settings.OAUTH2_PROVIDER_APPLICATION_MODEL),
    ]

    operations = [
        migrations.RunPython(rename_to_ehr_patient_portal, restore_patient_access_names),
    ]
