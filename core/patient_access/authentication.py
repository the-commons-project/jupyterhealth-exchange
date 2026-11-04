"""Patient sessions for the USCDI Patient Access client.

The invitation link logs the patient in (a Django session). This client's views, and the four
existing API views its shared screens call, accept that session beside their OAuth tokens. Only
a *patient's* session authenticates here: a practitioner's or an admin's browser session gets
nothing from these classes, so adding them widens nothing for anyone else. DRF's session class
enforces CSRF for every unsafe method.
"""

from __future__ import annotations

from typing import Any

from rest_framework.authentication import SessionAuthentication
from rest_framework.request import Request


class PatientSessionAuthentication(SessionAuthentication):
    def authenticate(self, request: Request) -> tuple[Any, None] | None:
        result = super().authenticate(request)
        if result is None:
            return None
        user, _ = result
        return result if getattr(user, "is_patient", lambda: False)() else None


class PatientSessionReadAuthentication(PatientSessionAuthentication):
    """For views whose writes stay token-only: the session authenticates reads alone."""

    def authenticate(self, request: Request) -> tuple[Any, None] | None:
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return None
        return super().authenticate(request)
