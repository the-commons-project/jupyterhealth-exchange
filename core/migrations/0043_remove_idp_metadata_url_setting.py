from django.db import migrations


def delete_superseded_idp_metadata_url_setting(apps, schema_editor):
    # IdP config moved to an allauth SocialApp row (docs/rfcs/0003); nothing
    # reads this key anymore, so drop it from already-seeded databases where it
    # would otherwise linger as an editable-but-dead settings-admin row. The
    # seeded value was always "" (SAML was never operable), so nothing is lost.
    JheSetting = apps.get_model("core", "JheSetting")
    JheSetting.objects.filter(key="auth.sso.idp_metadata_url").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0042_ehrbrand_ehrbrandlocation"),
    ]

    operations = [
        migrations.RunPython(
            delete_superseded_idp_metadata_url_setting,
            migrations.RunPython.noop,
        ),
    ]
