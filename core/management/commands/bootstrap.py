"""Make a migrated database usable. Idempotent: run it after every `migrate`.

Creates what is missing and never changes an existing row (except the MCP broker client, whose
credentials come from the environment and are updated when given): the runtime settings at
neutral values, the codeable concepts, data sources and JHE clients, the root organization, the
`JHE Admin UI` OAuth application the browser login needs, the EHR vendors with no credentials,
and the MCP broker client when MCP_OAUTH_CLIENT_ID and MCP_OAUTH_CLIENT_SECRET are set.

It creates no user and no demo data, and sets no registration invite code: registration stays
closed until an admin sets `site.registration_invite_code`. `seed` is this plus the
development demo.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from core import bootstrap


class Command(BaseCommand):
    help = "Create the reference data and the web UI's OAuth client a fresh database lacks (idempotent)."

    STEPS = [
        ("settings", bootstrap.ensure_settings),
        ("codeable concepts", bootstrap.ensure_codeable_concepts),
        ("data sources", bootstrap.ensure_data_sources),
        ("clients", bootstrap.ensure_clients),
        ("root organization", bootstrap.ensure_root_organization),
        ("admin UI client", bootstrap.ensure_admin_ui_application),
        ("EHR vendors", bootstrap.ensure_ehr_vendors),
        ("MCP broker client", bootstrap.ensure_mcp_broker_application),
    ]

    def handle(self, *args: Any, **options: Any) -> None:
        with transaction.atomic():
            for label, step in self.STEPS:
                created = step()
                if created is None:
                    detail = self.style.WARNING("skipped: MCP_OAUTH_CLIENT_ID/SECRET not set")
                elif created:
                    detail = f"created {len(created)}: {', '.join(created)}"
                else:
                    detail = "nothing to create"
                self.stdout.write(f"  {label.ljust(20)}{detail}")
        self.stdout.write(self.style.SUCCESS("Bootstrap complete."))
