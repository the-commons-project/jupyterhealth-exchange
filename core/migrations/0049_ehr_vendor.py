# EhrBrand.vendor was a free-text CharField (always "epic" in practice, since nothing ever set
# it otherwise). It becomes a ForeignKey to a new EhrVendor model, since JHE's OAuth client id
# and supported scopes are really per-vendor (one Epic App Orchard registration works across
# every hospital running Epic, discovered per-brand via the SMART `iss`) rather than per-brand.
#
# The rename-then-backfill-then-drop dance below exists because the old `vendor` column has
# real data in it (every deployed EhrBrand row): renaming it out of the way first, backfilling
# the new FK from its values, then dropping it, is what makes this safe to run against a
# database that already has EhrBrand rows -- not just a fresh one.

from django.db import migrations, models
import django.db.models.deletion


def _vendor_display_name(raw):
    raw = (raw or "").strip()
    return raw.title() if raw else "Unknown"


def migrate_vendor_forward(apps, schema_editor):
    EhrBrand = apps.get_model("core", "EhrBrand")
    EhrVendor = apps.get_model("core", "EhrVendor")
    vendor_by_name = {}
    for brand in EhrBrand.objects.all():
        name = _vendor_display_name(brand.vendor_legacy)
        vendor = vendor_by_name.get(name)
        if vendor is None:
            vendor, _ = EhrVendor.objects.get_or_create(name=name)
            vendor_by_name[name] = vendor
        brand.vendor = vendor
        brand.save(update_fields=["vendor"])


def migrate_vendor_backward(apps, schema_editor):
    EhrBrand = apps.get_model("core", "EhrBrand")
    for brand in EhrBrand.objects.select_related("vendor").all():
        brand.vendor_legacy = brand.vendor.name if brand.vendor else ""
        brand.save(update_fields=["vendor_legacy"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0048_ehr_brand_field_consistency_and_source_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="EhrVendor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(unique=True)),
                ("ehr_client_id", models.CharField(blank=True, null=True)),
                ("supported_scopes", models.TextField(blank=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.RenameField(model_name="ehrbrand", old_name="npi", new_name="npi_type_2"),
        migrations.RenameField(model_name="ehrbrand", old_name="vendor", new_name="vendor_legacy"),
        migrations.AddField(
            model_name="ehrbrand",
            name="vendor",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="brands",
                to="core.ehrvendor",
            ),
        ),
        migrations.RunPython(migrate_vendor_forward, migrate_vendor_backward),
        migrations.RemoveField(model_name="ehrbrand", name="vendor_legacy"),
        migrations.AlterField(
            model_name="ehrbrand",
            name="vendor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="brands",
                to="core.ehrvendor",
            ),
        ),
    ]
