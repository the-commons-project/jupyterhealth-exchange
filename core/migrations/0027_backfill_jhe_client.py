from django.conf import settings
from django.db import migrations


def backfill_jhe_clients(apps, schema_editor):
    Application = apps.get_model(settings.OAUTH2_PROVIDER_APPLICATION_MODEL)
    JheClient = apps.get_model("core", "JheClient")
    JheSetting = apps.get_model("core", "JheSetting")

    for app in Application.objects.all():
        jhe_client, _ = JheClient.objects.get_or_create(application=app)

        setting = JheSetting.objects.filter(setting_id=app.id, key="client.invitation_url").first()
        if setting and setting.value_string:
            jhe_client.invitation_url = setting.value_string
            jhe_client.save()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_jheclient"),
        migrations.swappable_dependency(settings.OAUTH2_PROVIDER_APPLICATION_MODEL),
    ]

    operations = [
        migrations.RunPython(backfill_jhe_clients, migrations.RunPython.noop),
    ]
