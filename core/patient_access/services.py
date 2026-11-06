"""JHE's own writes for a pull, made in the callback's transaction."""

from __future__ import annotations

from core.models import Patient, PatientIdentifier


class IdentifierConflict(Exception):
    """The EHR patient is already linked to another JHE patient. Safe to show the patient."""


def attach_patient_identifier(patient: Patient, system: str, value: str) -> None:
    """Additively link the EHR's patient id to this JHE patient, as the browser client does
    (`save_patient_identifier`): idempotent, never replaces another identifier, refuses one that
    belongs to someone else."""
    identifier, _ = PatientIdentifier.objects.get_or_create(system=system, value=value, defaults={"patient": patient})
    if identifier.patient_id != patient.id:
        raise IdentifierConflict(
            "This health record is already linked to another person in this study. Please contact your study team."
        )
