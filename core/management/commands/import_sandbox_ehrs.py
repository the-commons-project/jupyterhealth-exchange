"""
Load the EHR vendors' sandbox (test) environments for USCDI Patient Access: for each, the vendor
row and its one brand, so the hospital picker offers it ("Epic Sandbox", "Oracle Health Sandbox",
"eClinicalWorks Sandbox", "athenahealth Sandbox").

`import_ehr_brands` skips any brand with "sandbox" in its name (a patient should not be offered a
test environment), so this is the way to add them on purpose, on a staging or development
deployment. The list is core/data/ehr_sandboxes.json; edit it to add another vendor's.

    python manage.py import_sandbox_ehrs
    python manage.py import_sandbox_ehrs --client-id "Epic Sandbox=<id>" --client-id "athenahealth Sandbox=<id>"
    python manage.py import_sandbox_ehrs --only "Oracle Health Sandbox"

Client ids are never in the file: each belongs to one deployment's app registration. `--client-id`
sets one on its row (repeatable); without it a row's client id is left as it is, so the command is
safe to rerun. A row is created with EhrVendor.defaults_for its name (Epic's and Oracle Health's
sign-in settings); an existing row's settings are never changed. A brand already in the table is
kept, and moved to the sandbox's vendor row if it was under another. A brand with no location gets
one so it appears in the picker.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import EhrBrand, EhrBrandLocation, EhrVendor

DEFAULT_FILE = Path("core") / "data" / "ehr_sandboxes.json"


def _url_variants(url):
    bare = url.rstrip("/")
    return [url, bare, bare + "/"]


@transaction.atomic
def import_sandboxes(entries, client_ids=None):
    """Upsert each sandbox's vendor row and brand. Idempotent. Returns one result dict per entry:
    {"vendor", "brand", "created_vendor", "created_brand", "moved", "client_id_set"}."""
    client_ids = client_ids or {}
    results = []
    for entry in entries:
        vendor_name = entry["vendor"]
        vendor, created_vendor = EhrVendor.objects.get_or_create(
            name=vendor_name, defaults=EhrVendor.defaults_for(vendor_name)
        )
        client_id = client_ids.get(vendor_name)
        if client_id:
            vendor.ehr_client_id = client_id
            vendor.save(update_fields=["ehr_client_id", "last_updated"])

        url = entry["fhir_base_url"]
        brand = EhrBrand.objects.filter(fhir_base_url__in=_url_variants(url)).first()
        created_brand = brand is None
        moved = False
        if brand is None:
            brand = EhrBrand.objects.create(
                name=entry["brand"], vendor=vendor, fhir_base_url=url, fhir_version=entry.get("fhir_version", "R4")
            )
        elif brand.vendor_id != vendor.id:
            brand.vendor = vendor
            brand.save(update_fields=["vendor", "last_updated"])
            moved = True
        if not brand.locations.exists():
            EhrBrandLocation.objects.create(
                brand=brand,
                name=entry["brand"],
                city=entry.get("city"),
                state=entry.get("state"),
                country=entry.get("country", "US"),
            )
        results.append(
            {
                "vendor": vendor_name,
                "brand": brand.name,
                "created_vendor": created_vendor,
                "created_brand": created_brand,
                "moved": moved,
                "client_id_set": bool(client_id),
                "has_client_id": bool(vendor.ehr_client_id),
            }
        )
    return results


def _parse_client_ids(pairs):
    out = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip() or not value.strip():
            raise CommandError(f'--client-id takes "<vendor row name>=<client id>", got "{pair}"')
        out[name.strip()] = value.strip()
    return out


class Command(BaseCommand):
    help = "Load the vendor sandbox environments (vendor rows and brands) from core/data/ehr_sandboxes.json."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_FILE), help=f"Sandbox list (default: {DEFAULT_FILE}).")
        parser.add_argument(
            "--client-id",
            action="append",
            default=[],
            metavar="ROW=ID",
            help='Set the client id on a vendor row, e.g. "Epic Sandbox=<id>". Repeatable.',
        )
        parser.add_argument(
            "--only", action="append", default=[], metavar="ROW", help="Only these vendor rows. Repeatable."
        )

    def handle(self, *args, **options):
        with open(options["file"], encoding="utf-8") as f:
            entries = json.load(f)["sandboxes"]
        known = {e["vendor"] for e in entries}
        client_ids = _parse_client_ids(options["client_id"])
        for name in [*client_ids, *options["only"]]:
            if name not in known:
                raise CommandError(f'"{name}" is not in {options["file"]} (rows: {", ".join(sorted(known))})')
        if options["only"]:
            entries = [e for e in entries if e["vendor"] in options["only"]]

        for r in import_sandboxes(entries, client_ids):
            parts = [
                "vendor created" if r["created_vendor"] else "vendor exists",
                "brand created" if r["created_brand"] else "brand exists",
            ]
            if r["moved"]:
                parts.append("brand moved to this vendor row")
            parts.append(
                "client id set" if r["client_id_set"] else "client id " + ("kept" if r["has_client_id"] else "EMPTY")
            )
            line = f"{r['vendor']}: {', '.join(parts)}"
            self.stdout.write(self.style.WARNING(line) if not r["has_client_id"] else self.style.SUCCESS(line))
