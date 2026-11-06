"""The USCDI Patient Access client's session: started by its invitation link, accepted only by its
own endpoints.

The invitation link logs the patient in (a Django session) and marks that session as this
client's, for that patient (`mark_session`). The client's endpoints accept a request only when
the session carries the mark, the mark names the logged-in user's patient, and the user is a
patient and nothing else: a practitioner's or an admin's ordinary login never carries the mark,
and a user who is both practitioner and patient is refused even with it. JHE's shared API routes
stay token-only; nothing here is added to them. DRF's session class enforces CSRF for every
unsafe method.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from pydantic import BaseModel, ConfigDict, ValidationError
from rest_framework.authentication import SessionAuthentication
from rest_framework.request import Request

SESSION_MARK = "uscdi_patient_access"


class SessionMark(BaseModel):
    """What the invitation link records in the server-side session it starts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    patient_id: int


def mark_session(request: HttpRequest, patient_id: int) -> None:
    """After `login()` (which rotates the session key, so the mark cannot ride on an older one)."""
    request.session[SESSION_MARK] = SessionMark(patient_id=patient_id).model_dump()


def session_mark(request: HttpRequest | Request) -> SessionMark | None:
    try:
        raw = request.session.get(SESSION_MARK)
        return SessionMark.model_validate(raw) if raw is not None else None
    except ValidationError:
        return None


def is_patient_only(user: Any) -> bool:
    """A patient and nothing else: not a practitioner, not staff, not a superuser."""
    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_patient()
        and not user.is_practitioner()
        and not user.is_staff
        and not user.is_superuser
    )


class InvitationSessionAuthentication(SessionAuthentication):
    """The session the invitation link started, for the patient it was issued to. A request that
    carries a bearer token is not this class's to judge: it stands aside, so a dead token gets
    the token class's 401 instead of falling through to whichever patient the browser holds."""

    def authenticate(self, request: Request) -> tuple[Any, None] | None:
        if request.META.get("HTTP_AUTHORIZATION"):
            return None
        result = super().authenticate(request)
        if result is None:
            return None
        user, _ = result
        mark = session_mark(request)
        patient = user.get_patient() if is_patient_only(user) else None
        if mark is None or patient is None or patient.id != mark.patient_id:
            return None
        return result
