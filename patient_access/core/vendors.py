"""The vendor registry: what we know about each EHR vendor, the same in every deployment.

A deployment's part is only its client id per vendor and environment, which the host's directory
supplies (JHE: the `EhrVendor` row). Adding a vendor is an entry here, a profile in
`profiles.py` if its query table differs, and tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from .credentials import PUBLIC, AuthMethod
from .models import Value
from .profiles import EPIC_PROFILE, SMART_REFERENCE_PROFILE
from .source_settings import ClientRegistration, FetchProfile, SourceSettings
from .vocab import ScopeVersion, TypeChoice


class VendorKey(StrEnum):
    EPIC = "epic"
    SMART_REFERENCE = "smart-reference"  # the SMART Health IT sandbox: dev and end-to-end runs


class Vendor(Value):
    key: VendorKey
    fetch: FetchProfile
    scope_version: ScopeVersion = ScopeVersion.V1
    base_scopes: tuple[str, ...]
    methods: tuple[AuthMethod, ...] = (PUBLIC,)  # what this build registers: public clients
    type_choice: TypeChoice = TypeChoice.HOST

    def settings(self, *, vendor_name: str, client_id: str) -> SourceSettings:
        """This vendor's settings for one registration (the host's vendor name and client id)."""
        return SourceSettings(
            registration=ClientRegistration(
                vendor=vendor_name,
                client_id=client_id,
                methods=self.methods,
                base_scopes=self.base_scopes,
                scope_version=self.scope_version,
            ),
            fetch=self.fetch,
            type_choice=self.type_choice,
        )


VENDORS: Mapping[VendorKey, Vendor] = MappingProxyType(
    {
        VendorKey.EPIC: Vendor(
            key=VendorKey.EPIC,
            fetch=EPIC_PROFILE,
            # What JHE's EhrVendor.BASE_SCOPES requests from Epic today.
            base_scopes=("openid", "profile", "launch/patient"),
            type_choice=TypeChoice.EHR,
        ),
        VendorKey.SMART_REFERENCE: Vendor(
            key=VendorKey.SMART_REFERENCE,
            fetch=SMART_REFERENCE_PROFILE,
            base_scopes=("openid", "fhirUser", "launch/patient"),
        ),
    }
)
