import inspect
import logging
from datetime import datetime

import requests
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.crypto import get_random_string
from oauth2_provider.models import Grant, get_application_model
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from core.admin_pagination import CustomPageNumberPagination
from core.fhir_pagination import FHIRBundlePagination
from core.models import (
    CodeableConcept,
    JheSetting,
    JheUser,
    Observation,
    Organization,
    Patient,
    PatientOrganization,
    Practitioner,
    PractitionerOrganization,
    Study,
    StudyPatient,
    StudyPatientScopeConsent,
)
logger = logging.getLogger(__name__)

from core.permissions import IfUserCan
from core.services.ow_integration import ow_service
from core.serializers import (
    ClientSerializer,
    CodeableConceptSerializer,
    FHIRBundledPatientSerializer,
    PatientSerializer,
    StudyConsentsSerializer,
    StudyPatientScopeConsentSerializer,
    StudyPendingConsentsSerializer,
)


class PatientViewSet(ModelViewSet):
    model_class = Patient
    serializer_class = PatientSerializer
    pagination_class = CustomPageNumberPagination

    supported_query_params = {
        key
        for key in inspect.signature(Patient.for_practitioner_organization_study).parameters
        if key not in {"jhe_user_id"}
    }

    def get_permissions(self):
        """
        Instantiates and returns the list of permissions that this view requires.
        """
        if self.action in ["create", "destroy", "update", "partial_update"]:
            return [IfUserCan("patient.manage_for_organization")()]
        return [permission() for permission in self.permission_classes]

    def get_queryset(self):
        if self.detail:
            # if this is any practitioner (they don't need to be authorized just to view Patient details) or if this is
            # the patient accessing themselves
            if self.request.user.is_practitioner() or (
                self.request.user.get_patient() and self.request.user.get_patient().id == int(self.kwargs["pk"])
            ):
                return Patient.objects.filter(pk=self.kwargs["pk"])
            else:
                raise PermissionDenied("Current User does not have authorization to access this Patient.")
        else:
            return Patient.for_practitioner_organization_study(
                self.request.user.id,
                **{
                    key: value for key, value in self.request.query_params.items() if key in self.supported_query_params
                },
            )

    def create(self, request, *args, **kwargs):
        patient = None
        jhe_user = None
        if request.data["telecom_email"]:
            jhe_users = JheUser.objects.filter(email=request.data["telecom_email"])
            if jhe_users:
                jhe_user = jhe_users[0]
            else:
                jhe_user = JheUser(email=request.data["telecom_email"])
                jhe_user.set_password(get_random_string(length=16))
                jhe_user.save()
            request.data["jhe_user_id"] = jhe_user.id
            del request.data["telecom_email"]
            patient = Patient.objects.create(**request.data)
        else:
            raise ValidationError

        serializer = PatientSerializer(patient)
        return Response(serializer.data)

    def destroy(self, request, pk=None, *args, **kwargs):
        if organization_id := request.query_params.get("organization_id"):
            patient = self.get_object()
            PatientOrganization.objects.filter(patient=patient, organization_id=organization_id).delete()

            StudyPatientScopeConsent.objects.filter(
                study_patient__patient=patient,
                study_patient__study__organization_id=organization_id,
            ).delete()

            StudyPatient.objects.filter(patient=patient, study__organization_id=organization_id).delete()

            if not PatientOrganization.objects.filter(patient=patient).exists():
                Observation.objects.filter(subject_patient=patient).delete()
                patient.delete()

                if user := patient.jhe_user:
                    if not Practitioner.objects.filter(jhe_user_id=user.id).exists():
                        user.delete()

            return Response({"success": True})
        return Response({"detail": "organizationId required"}, status=status.HTTP_400_BAD_REQUEST)

    # These global methods (no premission checks) are for adding an existing patient
    # to another Organization (the exact email must be known)
    @action(detail=False, methods=["GET"])
    def global_lookup(self, request):
        email = request.GET.get("email")
        if not email:
            raise ValidationError("email parameter required")
        patients = Patient.objects.filter(jhe_user__email=email)
        return Response(PatientSerializer(patients, many=True).data, status=200)

    @action(detail=True, methods=["PATCH"])
    def global_add_organization(self, request, pk):
        organization_id = request.GET.get("organization_id")
        if not organization_id:
            raise ValidationError("organizationId parameter required")
        patient = self.get_object()
        organization = Organization.objects.get(pk=organization_id)
        if not organization:
            raise ValidationError("Organization could not be found")
        PatientOrganization.objects.create(organization_id=organization.id, patient_id=patient.id)
        return Response(PatientSerializer(patient, many=False).data, status=200)

    @action(detail=True, methods=["GET"])
    def consolidated_clients(self, request, pk):
        patient = self.get_object()  # Patient instance
        Client = get_application_model()

        grants_for_patient_user = Grant.objects.filter(user_id=patient.jhe_user_id)

        code_verifier_subquery = JheSetting.objects.filter(
            setting_id=OuterRef("id"), key="client.code_verifier"
        ).values("value_string")[:1]

        invitation_url_subquery = JheSetting.objects.filter(
            setting_id=OuterRef("id"), key="client.invitation_url"
        ).values("value_string")[:1]

        patient_clients = (
            Client.objects.filter(studies__study__studypatient__patient_id=pk)
            .annotate(code_verifier=Subquery(code_verifier_subquery), invitation_url=Subquery(invitation_url_subquery))
            .prefetch_related(Prefetch("grant_set", queryset=grants_for_patient_user, to_attr="patient_grants"))
            .distinct()
        )

        serializer = ClientSerializer(patient_clients, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["GET"])
    def invitation_link(self, request, pk):
        client_id = request.query_params.get("application_id")
        Client = get_application_model()
        send_email = request.query_params.get("send_email") == "true"
        patient = self.get_object()

        if not client_id:
            raise APIException("Missing required query parameter: application_id")

        client_client_id = Client.objects.get(pk=client_id).client_id

        if not patient:
            raise APIException("Patient not found.")

        code_verifier_setting = JheSetting.objects.filter(setting_id=client_id, key="client.code_verifier").first()

        if not code_verifier_setting:
            raise APIException("Missing JheSetting: client.code_verifier")

        invitation_url_setting = JheSetting.objects.filter(setting_id=client_id, key="client.invitation_url").first()

        if not invitation_url_setting:
            raise APIException("Missing JheSetting: client.invitation_url")

        grant = patient.jhe_user.create_authorization_code(
            client_id,
            code_verifier_setting.get_value(),
        )

        if not grant:
            raise APIException("Failed to create authorization code.")

        invitation_link = Patient.construct_invitation_link(
            invitation_url_setting.get_value(), client_client_id, grant.code, code_verifier_setting.get_value()
        )

        if send_email:
            message = render_to_string(
                "registration/invitation_email.html",
                {
                    "patient_name": patient.name_given,
                    "invitation_link": invitation_link,
                },
            )
            email = EmailMessage("JHE Invitation", message, to=[patient.jhe_user.email])
            email.content_subtype = "html"
            email.send()

        return Response({"invitation_link": invitation_link})

    @action(detail=True, methods=["GET", "POST", "PATCH", "DELETE"])
    def consents(self, request, pk):
        # if this is a patient, check they are accessing their own consents
        if (not request.user.is_practitioner()) and (int(pk) != request.user.get_patient().id):
            raise PermissionDenied("The Patient does not match the current patient user.")
        patient = self.get_object()
        if request.method == "GET":
            # if this is a practitioner, check they're authorized
            if (request.user.is_practitioner()) and not Patient.practitioner_authorized(request.user.id, int(pk)):
                raise PermissionDenied("This Practitioner not authorized to access this Patient")
            if self.request.GET.get("reset") == "true":  # used for dev an testing
                reset_count = 0
                for study_patient in StudyPatient.objects.filter(patient_id=int(pk)):
                    for study_patient_scope_consent in StudyPatientScopeConsent.objects.filter(
                        study_patient_id=study_patient.id
                    ):
                        study_patient_scope_consent.delete()
                        reset_count += 1
                return Response({"reset_count": reset_count})
            patient_serializer = PatientSerializer(patient, many=False)
            studies_pending_serializer = StudyPendingConsentsSerializer(
                Study.studies_with_scopes(int(pk), True), many=True
            )
            studies_serializer = StudyConsentsSerializer(Study.studies_with_scopes(int(pk), False), many=True)
            codeable_concept_serializer = CodeableConceptSerializer(patient.consolidated_consented_scopes(), many=True)
            return Response(
                {
                    "patient": patient_serializer.data,
                    "consolidated_consented_scopes": codeable_concept_serializer.data,
                    "studies_pending_consent": studies_pending_serializer.data,
                    "studies": studies_serializer.data,
                }
            )
        else:
            # if the user is the patient; or
            # the user is a practitioner and a member or manager of the organization that owns the study and patient; or
            # the user is a super admin

            responses = []
            consented_time = datetime.now()
            patient_user = request.user.get_patient()
            is_patient_user = bool(patient_user and int(pk) == patient_user.id)
            for study_scope_consent in request.data["study_scope_consents"]:
                study_patient = StudyPatient.objects.filter(
                    study_id=study_scope_consent["study_id"], patient_id=patient.id
                ).first()
                if not request.user.is_superuser and not is_patient_user:
                    if request.user.is_practitioner():
                        if not Patient.practitioner_authorized(
                            request.user.id, int(pk), organization_id=study_patient.study.organization.id
                        ):
                            raise PermissionDenied("Practitioner doesn't have right now for patient.")
                        practitioner_org = PractitionerOrganization.objects.filter(
                            organization=study_patient.study.organization.id,
                            practitioner=request.user.practitioner_profile,
                        ).first()
                        if practitioner_org.role not in ["manager", "member"]:
                            raise PermissionDenied("Practitioner role is not valid.")
                    else:
                        raise PermissionDenied("Only Patient users can update their own consents.")

                for scope_consent in study_scope_consent["scope_consents"]:
                    scope_coding_system = scope_consent["coding_system"]
                    scope_coding_code = scope_consent["coding_code"]
                    scope_code_id = CodeableConcept.objects.get(
                        coding_system=scope_coding_system, coding_code=scope_coding_code
                    ).id

                    if request.method == "POST":
                        responses.append(
                            StudyPatientScopeConsent.objects.create(
                                study_patient_id=study_patient.id,
                                scope_code_id=scope_code_id,
                                consented=scope_consent["consented"],
                                consented_time=consented_time,
                            )
                        )
                    elif request.method == "PATCH":
                        responses.append(
                            StudyPatientScopeConsent.objects.get(
                                study_patient_id=study_patient.id,
                                scope_code_id=scope_code_id,
                            ).update(
                                consented=scope_consent["consented"],
                                consented_time=consented_time,
                            )
                        )
                    elif request.method == "DELETE":
                        responses.append(
                            StudyPatientScopeConsent.objects.get(
                                study_patient_id=study_patient.id,
                                scope_code_id=scope_code_id,
                            ).delete()
                        )

            return Response({"study_scope_consents": StudyPatientScopeConsentSerializer(responses, many=True).data})

    @action(detail=False, methods=["get"], url_path="me")
    def me(self, request):
        """Return the patient record for the currently authenticated user."""
        patient = request.user.get_patient()
        if not patient:
            return Response({"detail": "Current user is not a patient."}, status=status.HTTP_404_NOT_FOUND)
        serializer = PatientSerializer(patient)
        return Response(serializer.data)

    @staticmethod
    def _get_ow_user_id(jhe_user) -> str | None:
        """Extract OW user ID from JheUser.identifier (format: 'ow:<uuid>')."""
        if jhe_user.identifier and jhe_user.identifier.startswith("ow:"):
            return jhe_user.identifier[3:]
        return None

    @staticmethod
    def _set_ow_user_id(jhe_user, ow_user_id: str) -> None:
        """Store OW user ID on JheUser.identifier with 'ow:' prefix."""
        jhe_user.identifier = f"ow:{ow_user_id}"
        jhe_user.save(update_fields=["identifier"])

    def _get_authorized_patient(self, pk):
        """Return the Patient with the given pk if and only if the current
        user is authorized to act on their wearable connection.

        Authorization rules:
            - The patient can act on themselves.
            - A practitioner who is a member/manager of any organization
              the patient belongs to can act on them.
            - Superusers can act on anyone.

        Raises:
            Http404: if no such patient exists.
            PermissionDenied: if the user is not authorized for this patient.
        """
        patient = get_object_or_404(Patient, pk=pk)
        user = self.request.user

        if user.is_superuser:
            return patient

        # Patient acting on self
        own_patient = user.get_patient()
        if own_patient and own_patient.id == patient.id:
            return patient

        # Practitioner authorized via shared org
        if user.is_practitioner():
            if Patient.practitioner_authorized(user.id, patient_id=patient.id):
                return patient

        raise PermissionDenied(
            "You are not authorized to act on this patient's wearable connection."
        )

    def _verify_patient_in_study(self, patient, study_id):
        """Confirm the patient is enrolled in the given study, else 404."""
        if not StudyPatient.objects.filter(patient=patient, study_id=study_id).exists():
            raise Http404("Patient is not enrolled in this study.")

    @action(
        detail=True,
        methods=["post"],
        url_path="wearable-redirect",
        permission_classes=[IsAuthenticated],
    )
    def wearable_redirect(self, request, pk=None):
        """Create OW user and return the wearable OAuth URL.

        Requires the caller to be either the patient themselves or a
        practitioner authorized for the patient's organization. The patient
        must also be enrolled in the requested study.

        With the polling architecture (v1), JHE pulls data from OW on a
        cron schedule — there is no push config to create. This action
        only needs to: (1) ensure the patient has an OW user, and
        (2) return the OAuth URL so the patient can authorize the provider.
        """
        patient = self._get_authorized_patient(pk)
        jhe_user = patient.jhe_user

        # Get study + data source + provider info from request
        study_id = request.data.get("study_id")
        data_source_id = request.data.get("data_source_id")
        provider = request.data.get("provider", "oura")
        redirect_uri = request.data.get(
            "redirect_uri", f"{request.build_absolute_uri('/')[:-1]}/ow/complete"
        )

        if not study_id or not data_source_id:
            return Response(
                {"detail": "study_id and data_source_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate provider to prevent path traversal into OW endpoints.
        if not provider.isalnum():
            return Response(
                {"detail": "Invalid provider name."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate redirect_uri to prevent open redirect / OAuth code theft.
        site_origin = request.build_absolute_uri("/")
        if not redirect_uri.startswith(site_origin):
            return Response(
                {"detail": "redirect_uri must be on this server."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify enrollment before doing anything that touches OW
        self._verify_patient_in_study(patient, study_id)

        # Find or create user in OW (returns existing if email matches).
        # Wrap in atomic + select_for_update to prevent two concurrent
        # wearable_redirect calls from creating duplicate OW users.
        with transaction.atomic():
            jhe_user_locked = JheUser.objects.select_for_update().get(pk=jhe_user.pk)
            ow_user_id = self._get_ow_user_id(jhe_user_locked)
            if not ow_user_id:
                ow_user_id = ow_service.find_or_create_user(
                    jhe_user_locked.email,
                    first_name=patient.name_given,
                    last_name=patient.name_family,
                    external_user_id=str(patient.id),
                )
                self._set_ow_user_id(jhe_user_locked, ow_user_id)

        # Get OAuth URL for the selected provider
        try:
            auth_url = ow_service.get_wearable_auth_url(ow_user_id, provider, redirect_uri)
        except requests.RequestException as e:
            logger.error("Failed to get OW auth URL for patient %s: %s", patient.id, e)
            return Response(
                {
                    "detail": (
                        "Failed to start the wearable authorization flow. "
                        "Please contact your study coordinator."
                    )
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"authorization_url": auth_url})

    @action(
        detail=True,
        methods=["get"],
        url_path="wearable-status",
        permission_classes=[IsAuthenticated],
    )
    def wearable_status(self, request, pk=None):
        """Check if patient has active wearable connections.

        Requires the caller to be the patient themselves or a practitioner
        authorized for one of the patient's organizations.
        """
        patient = self._get_authorized_patient(pk)
        ow_user_id = self._get_ow_user_id(patient.jhe_user)
        if not ow_user_id:
            return Response({"connected": False, "connections": []})
        try:
            connections = ow_service.check_connection_status(ow_user_id)
            return Response({"connected": len(connections) > 0, "connections": connections})
        except requests.RequestException as e:
            logger.warning("Failed to check OW connection status for patient %s: %s", patient.id, e)
            return Response({"connected": False, "connections": []})

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"consents/(?P<study_id>\d+)",
        permission_classes=[IsAuthenticated],
    )
    def revoke_consent(self, request, pk=None, study_id=None):
        """Revoke consent for a study and notify OW.

        Requires the caller to be the patient themselves or a practitioner
        authorized for one of the patient's organizations. The patient must
        also be enrolled in the requested study.
        """
        patient = self._get_authorized_patient(pk)
        self._verify_patient_in_study(patient, study_id)

        # Delete consent records so the study returns to "pending" state and
        # the patient can re-consent via a new invite link. The old approach
        # (setting consented=False) left orphan rows that the frontend treated
        # as "already consented" and the consent POST couldn't re-create
        # (UniqueViolation).
        with transaction.atomic():
            study_patients = StudyPatient.objects.filter(study_id=study_id, patient=patient)
            for sp in study_patients:
                StudyPatientScopeConsent.objects.filter(study_patient=sp).delete()

        # Revoke the OW vendor connection so OW stops pulling from the
        # provider. "Revoke" means "stop collecting," not just "stop showing."
        # Best-effort: JHE consent is already revoked so data cannot flow
        # into JHE regardless of whether this OW call succeeds.
        ow_user_id = self._get_ow_user_id(patient.jhe_user)
        if ow_user_id:
            try:
                # TODO: resolve provider from the study's DataSource instead
                # of hardcoding "oura" when multi-provider support is added.
                ow_service.revoke_connection(ow_user_id, provider="oura")
            except requests.RequestException as e:
                logger.warning("Failed to revoke OW vendor connection: %s", e)

        return Response({"status": "revoked"})


class FHIRPatientViewSet(ModelViewSet):
    serializer_class = FHIRBundledPatientSerializer
    pagination_class = FHIRBundlePagination

    def get_queryset(self):
        patient_identifier_system_and_value = self.request.GET.get("identifier", None)

        # GET /Patient?_has:Group:member:_id=<group-id>
        study_id = self.request.GET.get("_has:_group:member:_id", None)

        if not (study_id or patient_identifier_system_and_value):
            raise ValidationError(
                "Request parameter _has:Group:member:_id=<study_id> or"
                " patient.identifier=<system>|<value> must be provided."
            )

        patient_identifier_system = None
        patient_identifier_value = None
        if patient_identifier_system_and_value:
            patient_identifier_split = patient_identifier_system_and_value.split("|")  # TBD 400 for formatting error
            patient_identifier_system = patient_identifier_split[0]
            patient_identifier_value = patient_identifier_split[1]

        if study_id and (not Study.practitioner_authorized(self.request.user.id, study_id)):
            raise PermissionDenied("Current User does not have authorization to access this Study.")

        if patient_identifier_system_and_value and (
            not Patient.practitioner_authorized(self.request.user.id, None, None, patient_identifier_value)
        ):
            raise PermissionDenied("Current User does not have authorization to access this Patient.")

        return Patient.fhir_search(
            self.request.user.id,
            study_id,
            patient_identifier_system,
            patient_identifier_value,
        )
