"""SMART Health IT sandbox launch options.

Vendored from jupyterhealth/jupyter-smart-on-fhir (BSD-3) tests/conftest.py `SandboxConfig`, checked
against smart-on-fhir/smart-launcher-v2 src/isomorphic/codec.ts on 2026-10-22. Fixes on the way in:
the launcher decodes base64url, not standard base64; launch types and client types are the
codec's index tables; booleans are 0/1.

The sandbox reads its whole configuration from the URL, so a sandbox "brand" is just an iss:
    https://launch.smarthealthit.org/v/r4/sim/{encoded options}/fhir

A dataclass rather than a pydantic model: it mirrors the launcher's positional array one-to-one
and is only ever built in code by the dev seed and tests. Public-client options only are used.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

HOSTED = "https://launch.smarthealthit.org"

LAUNCH_TYPES = ["provider-ehr", "patient-portal", "provider-standalone", "patient-standalone", "backend-service"]
CLIENT_TYPES = ["public", "confidential-symmetric", "confidential-asymmetric", "backend-service"]
PKCE = ["none", "auto", "always"]


@dataclass
class SandboxConfig:
    launch_type: str = "patient-standalone"
    patient: list[str] = field(default_factory=list)  # preselect; empty shows the patient picker
    provider: list[str] = field(default_factory=list)
    encounter: str = "AUTO"
    skip_login: bool = False
    skip_auth: bool = False
    sim_ehr: bool = False
    scope: str = ""
    redirect_uris: list[str] = field(default_factory=list)
    client_id: str = ""
    client_secret: str = ""
    auth_error: str = ""
    jwks_url: str = ""
    jwks: str = ""
    client_type: str = "public"
    pkce: str = "auto"
    fhir_server: str = ""

    def encode(self) -> str:
        arr = [
            LAUNCH_TYPES.index(self.launch_type),
            ",".join(self.patient),
            ",".join(self.provider),
            self.encounter,
            int(self.skip_login),
            int(self.skip_auth),
            int(self.sim_ehr and "standalone" not in self.launch_type),
            self.scope,
            ",".join(self.redirect_uris),
            self.client_id,
            self.client_secret,
            self.auth_error,
            self.jwks_url,
            self.jwks,
            CLIENT_TYPES.index(self.client_type),
            PKCE.index(self.pkce),
            self.fhir_server,
        ]
        raw = json.dumps(arr, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def iss(self, base: str = HOSTED, fhir_version: str = "r4") -> str:
        return f"{base}/v/{fhir_version}/sim/{self.encode()}/fhir"
