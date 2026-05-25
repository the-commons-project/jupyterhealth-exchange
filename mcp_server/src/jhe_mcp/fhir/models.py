from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class StudyMeta(BaseModel):
    study_id: str
    name: str
    description: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None

    @classmethod
    def from_admin(cls, data: dict[str, Any]) -> StudyMeta:
        org = data.get("organization") or {}
        return cls(
            study_id=str(data["id"]),
            name=data["name"],
            description=data.get("description"),
            organization_id=str(org["id"]) if "id" in org else None,
            organization_name=org.get("name"),
        )


class Demographics(BaseModel):
    patient_id: str
    given_name: str | None = None
    family_name: str | None = None
    birth_date: str | None = None

    @classmethod
    def from_admin(cls, data: dict[str, Any]) -> Demographics:
        """Build from JHE Admin API `/api/v1/patients/{id}` response.

        JHE uses djangorestframework-camel-case middleware, so JSON field names
        are camelCase: `nameGiven`, `nameFamily`, `birthDate`.
        """
        return cls(
            patient_id=str(data["id"]),
            given_name=data.get("nameGiven"),
            family_name=data.get("nameFamily"),
            birth_date=data.get("birthDate"),
        )


class StudyPatient(BaseModel):
    patient_id: str
    given_name: str | None = None
    family_name: str | None = None
    email: str | None = None

    @classmethod
    def from_admin(cls, data: dict[str, Any]) -> StudyPatient:
        return cls(
            patient_id=str(data["id"]),
            given_name=data.get("nameGiven"),
            family_name=data.get("nameFamily"),
            email=data.get("telecomEmail"),
        )


class Observation(BaseModel):
    observation_id: str
    patient_id: str | None = None
    code: str | None = None
    code_system: str | None = None
    code_display: str | None = None
    effective_at: str | None = None
    value: float | None = None
    unit: str | None = None

    @classmethod
    def from_fhir_entry(cls, entry: dict[str, Any]) -> Observation:
        r = entry.get("resource") or entry
        coding = ((r.get("code") or {}).get("coding") or [{}])[0]
        qty = r.get("valueQuantity") or {}
        subj = (r.get("subject") or {}).get("reference") or ""
        patient_id = subj.split("/", 1)[1] if subj.startswith("Patient/") else None
        return cls(
            observation_id=str(r["id"]),
            patient_id=patient_id,
            code=coding.get("code"),
            code_system=coding.get("system"),
            code_display=coding.get("display"),
            effective_at=r.get("effectiveDateTime"),
            value=qty.get("value"),
            unit=qty.get("unit"),
        )
