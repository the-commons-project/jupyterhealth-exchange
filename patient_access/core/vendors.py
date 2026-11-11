"""The vendor registry: what we know about each EHR vendor, the same in every deployment.

A deployment's part is only its client id per vendor and environment, which the host's directory
supplies (JHE: the `EhrVendor` row). Adding a vendor is an entry here, a profile in
`profiles.py` if its query table differs, and tests.

A vendor with no entry of its own is not shut out: every certified EHR offers the same standard
sign-in and record types, and `US_CORE` is that baseline. An entry is for what a vendor does
differently, once it is known.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from .credentials import PUBLIC, AuthMethod
from .models import Value
from .profiles import EPIC_PROFILE, SMART_REFERENCE_PROFILE, USCDI_PROFILE
from .source_settings import ClientRegistration, FetchProfile, SourceSettings
from .vocab import ScopeVersion, TypeChoice


class VendorKey(StrEnum):
    EPIC = "epic"
    SMART_REFERENCE = "smart-reference"  # the SMART Health IT sandbox: dev and end-to-end runs
    US_CORE = "us-core"  # any certified EHR the registry has no entry for


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
        # What SMART App Launch and US Core ask of every certified EHR: the standard sign-in
        # scopes, the USCDI table as published, and our own checklist for the record types. A
        # vendor that differs somewhere gets a row that fails, not a pull that fails, until it
        # has an entry of its own.
        VendorKey.US_CORE: Vendor(
            key=VendorKey.US_CORE,
            fetch=USCDI_PROFILE,
            base_scopes=("openid", "fhirUser", "launch/patient"),
        ),
    }
)
