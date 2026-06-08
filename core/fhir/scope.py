"""Shared scoping/authorization helpers for the normalized ``Model.fhir_search`` methods.

Every FHIR-mapped Django model exposes a uniform::

    fhir_search(jhe_user_id, resource_id=None, organization_id=None,
                study_id=None, patient_id=None, **params)

The user is resolved from ``jhe_user_id`` (so callers/handlers never pass an ``is_patient``
flag): a **patient** user gets a self-scoped result that ignores the organization/study/patient
filters; a **practitioner** user gets the organization-membership-scoped result, with each
explicit filter authorized up front (an unauthorized organization/study/patient -> 403).

These two concerns are identical across all six models, so they live here rather than being
duplicated per model. Model imports are done lazily inside the functions to avoid import cycles
(the models import this module at class-definition time).
"""


def resolve_fhir_user(jhe_user_id):
    """Resolve the requesting ``JheUser`` (404 if it does not exist).

    Both role profiles are selected so the subsequent ``is_patient()`` check (which inspects
    ``patient_profile``) does not issue a second query -- a single lookup decides the branch.
    """
    from django.shortcuts import get_object_or_404

    from core.models import JheUser

    return get_object_or_404(JheUser.objects.select_related("patient_profile", "practitioner_profile"), id=jhe_user_id)


def authorize_practitioner_scope(
    jhe_user_id,
    organization_id=None,
    study_id=None,
    patient_id=None,
):
    """Authorize a practitioner's explicit *targeted* search filters, raising 403 on a mismatch.

    A request that names a concrete organization/study/patient the practitioner has no access
    to is rejected: an organization they do not belong to, a study under an organization they
    are not in, or a patient who shares no organization with them all raise ``PermissionDenied``.
    A paramless call is a no-op (the caller returns the full organization-shared set).

    An ``identifier`` filter is deliberately *not* authorized here -- it is a search predicate,
    not a targeted resource, and the organization-membership join already scopes the result, so
    a non-matching or unauthorized identifier simply yields an empty set rather than a 403.
    """
    from django.core.exceptions import PermissionDenied

    from core.models import Organization, Patient, Study

    # Each check is a single membership ``.exists()`` against the practitioner's organizations
    # (no Practitioner row is materialized -- this runs on both sources of a mapped+aux union
    # search, so it stays one query apiece).
    if (
        organization_id
        and not Organization.objects.filter(id=organization_id, practitioners__jhe_user_id=jhe_user_id).exists()
    ):
        raise PermissionDenied(f"Current user is not authorized to access Organization/{organization_id}.")
    if (
        study_id
        and not Study.objects.filter(id=study_id, organization__practitioners__jhe_user_id=jhe_user_id).exists()
    ):
        raise PermissionDenied(f"Current user is not authorized to access Group/{study_id}.")
    if (
        patient_id
        and not Patient.objects.filter(id=patient_id, organizations__practitioners__jhe_user_id=jhe_user_id).exists()
    ):
        raise PermissionDenied(f"Current user is not authorized to access Patient/{patient_id}.")
