from django.conf import settings
from django.core.mail import EmailMessage
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from oauth2_provider.models import get_application_model
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from core.context_processors import DEFAULT_LOGO
from core.models import Patient, PatientInvitation, Study
from core.models.patient_invitation import InvitationCancelled, InvitationConflict, InvitationExpired
from core.permissions import can_manage_patient, patients_manageable_by
from core.serializers import PatientInvitationSerializer
from core.services.jhe_settings import get_setting

Application = get_application_model()


def _single_pending_study_name(patient):
    # Name of the patient's single pending study, or None (the email then uses generic copy).
    studies = Study.studies_with_scopes(patient.id, pending=True)
    return studies[0].name if len(studies) == 1 else None


class PatientInvitationViewSet(ModelViewSet):
    serializer_class = PatientInvitationSerializer

    def get_permissions(self):
        if self.action == "redeem":
            return [AllowAny()]
        return super().get_permissions()

    # No PUT or PATCH: nothing edits an invitation (its status changes by redeeming, reissuing or expiring).
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        # Only the invitations of patients the caller may manage; any other id is a 404.
        return PatientInvitation.objects.filter(patient__in=patients_manageable_by(self.request.user))

    def create(self, request, *args, **kwargs):
        patient_id = request.data.get("patient_id")
        client_id = request.data.get("client_id")
        send_email = request.data.get("send_email") == True  # noqa: E712

        errors = {}
        if not patient_id:
            errors["patient_id"] = ["This field is required."]
        if not client_id:
            errors["client_id"] = ["This field is required."]
        if errors:
            raise ValidationError(errors)

        # An invitation link signs whoever opens it in as the patient, so it is issued only by someone
        # who may manage that patient (see can_manage_patient). A patient that does not exist answers
        # the same as one the caller may not manage, so the endpoint does not reveal which ids exist.
        patient = Patient.objects.filter(id=patient_id).first()
        if patient is None or not can_manage_patient(request.user, patient):
            raise PermissionDenied("You are not allowed to invite this patient.")
        client = get_object_or_404(Application, id=client_id)

        invitation, link = PatientInvitation.build_link(patient, client)

        if send_email:
            message = render_to_string(
                "registration/invitation_email.html",
                {
                    "patient_name": patient.name_given,
                    "invitation_link": link,
                    "site_url": get_setting("site.url", settings.SITE_URL),
                    "site_title": get_setting("site.ui.title"),
                    "site_logo": get_setting("site.ui.logo", "") or DEFAULT_LOGO,
                    "study_name": _single_pending_study_name(patient),
                },
            )
            email = EmailMessage("You're invited to share your health data", message, to=[patient.jhe_user.email])
            email.content_subtype = "html"
            email.send()

        data = PatientInvitationSerializer(invitation).data
        data["invitation_link"] = link
        return Response(data, status=status.HTTP_201_CREATED)

    # Token is used both for lookup and as PKCE code verifier
    @action(detail=False, methods=["POST"], url_path=r"(?P<token>[A-Za-z0-9]{32})")
    def redeem(self, request, token):
        try:
            _, grant = PatientInvitation.redeem(token)
        except PatientInvitation.DoesNotExist:
            raise NotFound("Invitation not found or already used.")
        except InvitationExpired:
            return Response({"detail": "Invitation has expired."}, status=410)
        except InvitationCancelled:
            return Response({"detail": "Invitation has been cancelled."}, status=403)
        except InvitationConflict:
            return Response({"detail": "Invitation has already been used."}, status=409)
        except ValueError as e:
            raise ValidationError(str(e))

        return Response(
            {
                "grant": {
                    "grant_type": "authorization_code",
                    "redirect_uri": grant.redirect_uri,
                    "client_id": grant.application.client_id,
                    "code": grant.code,
                },
                "token_endpoint": request.build_absolute_uri(reverse("oauth2_provider:token")),
                "expires": grant.expires,
            }
        )
