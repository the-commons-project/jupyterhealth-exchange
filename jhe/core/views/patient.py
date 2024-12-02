import logging
from core.serializers import CodeableConceptSerializer, FHIRBundledPatientSerializer, PatientSerializer, StudyPendingConsentsSerializer, StudyConsentsSerializer, StudyPatientScopeConsentSerializer
from core.models import JheUser, CodeableConcept, Patient, StudyPatient, StudyPatientScopeConsent, Study
from core.utils import FHIRBundlePagination
from rest_framework.viewsets import ModelViewSet
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from django.utils.crypto import get_random_string
from rest_framework.decorators import action
from django.conf import settings
from django.core.exceptions import PermissionDenied, BadRequest
from datetime import datetime

logger = logging.getLogger(__name__)

class PatientViewSet(ModelViewSet):
    
    serializer_class = PatientSerializer

    def get_queryset(self):
        if self.detail:
            # if this is the patient accessing themselves, or an authorized practitioner
            if (self.request.user.get_patient() and self.request.user.get_patient().id==int(self.kwargs['pk'])) or Patient.practitioner_authorized(self.request.user.id, self.kwargs['pk']):
                return Patient.objects.filter(id=self.kwargs['pk'])
            else:
                raise PermissionDenied("Current User does not have authorization to access this Patient.")
        else:
            return Patient.for_practitioner_organization_study(self.request.user.id, self.request.GET.get('organization_id', None), self.request.GET.get('study_id', None))

    def create(self, request):
        patient = None
        jhe_user = None
        if request.data['telecom_email']:
            jhe_users = JheUser.objects.filter(email=request.data['telecom_email'])
            if jhe_users:
                jhe_user = jhe_users[0]
            else:
                jhe_user = JheUser(email=request.data['telecom_email'])
                jhe_user.set_password(get_random_string(length=16))
                jhe_user.save()
            request.data['jhe_user_id'] = jhe_user.id
            del request.data['telecom_email']
            patient = Patient.objects.create(**request.data)
        else:
            raise ValidationError
        
        serializer = PatientSerializer(patient)
        return Response(serializer.data)
    
    def destroy(self, request, pk=None):
        patient = self.get_object()
        patient.delete()
        return Response({'success': True})
    
    @action(detail=True, methods=['GET'])
    def invitation_link(self, request, pk):
        patient = self.get_object()
        grant = patient.jhe_user.create_authorization_code(1,settings.OIDC_CLIENT_REDIRECT_URI)
        return Response({"invitation_link": settings.CH_INVITATION_LINK_PREFIX+settings.SITE_URL.split('/')[2]+'|'+grant.code})

    @action(detail=True, methods=['GET','POST','PATCH','DELETE'])
    def consents(self, request, pk):
        # if this is a patient, check they are accessing their own consents
        if (request.user.get_patient() != None) and (int(pk) != request.user.get_patient().id):
            raise PermissionDenied("The Patient does not match the current patient user.")
        patient = self.get_object()
        if request.method == 'GET':
            # if this is a practitioner, check they're authorized
            if (request.user.get_patient() == None) and not Patient.practitioner_authorized(request.user.id,int(pk)):
                raise PermissionDenied("This Practitioner not authorized to access this Patient")
            if self.request.GET.get('reset')=='true': # used for dev an testing
                reset_count = 0
                for study_patient in StudyPatient.objects.filter(patient_id=int(pk)):
                    for study_patient_scope_consent in StudyPatientScopeConsent.objects.filter(study_patient_id=study_patient.id):
                        study_patient_scope_consent.delete()
                        reset_count += 1
                return Response({"reset_count": reset_count})
            patient_serializer = PatientSerializer(patient, many=False)
            studies_pending_serializer = StudyPendingConsentsSerializer(Study.studies_with_scopes(int(pk), True), many=True)
            studies_serializer = StudyConsentsSerializer(Study.studies_with_scopes(int(pk), False), many=True)
            codeable_concept_serializer = CodeableConceptSerializer(patient.consolidated_consented_scopes(), many=True)
            return Response({
                "patient": patient_serializer.data,
                "consolidated_consented_scopes": codeable_concept_serializer.data,
                "studies_pending_consent": studies_pending_serializer.data,
                "studies": studies_serializer.data
            })
        else:
            # if this is a practitioner they can only read consents not write
            if (request.user.get_patient() == None):
                raise PermissionDenied("Only Patient users can update their own consents.")
            responses = []
            consented_time = datetime.now()
            for study_scope_consent in request.data['study_scope_consents']:
                study_patient = StudyPatient.objects.filter(study_id=study_scope_consent["study_id"], patient_id=patient.id).first()
                for scope_consent in study_scope_consent["scope_consents"]:

                    scope_coding_system = scope_consent['coding_system']
                    scope_coding_code = scope_consent['coding_code']
                    scope_code_id = CodeableConcept.objects.get(coding_system=scope_coding_system, coding_code=scope_coding_code).id

                    if request.method == 'POST':
                        responses.append(
                            StudyPatientScopeConsent.objects.create(
                                study_patient_id=study_patient.id,
                                scope_code_id=scope_code_id,
                                consented=scope_consent["consented"],
                                consented_time=consented_time,
                            )
                        )
                    elif request.method == 'PATCH':
                        responses.append(
                            StudyPatientScopeConsent.objects.get(
                                study_patient_id=study_patient.id,
                                scope_code_id=scope_code_id,
                            ).update(
                                consented=scope_consent["consented"],
                                consented_time=consented_time,
                            )
                        )
                    elif request.method == 'DELETE':
                        responses.append(
                            StudyPatientScopeConsent.objects.get(
                                study_patient_id=study_patient.id,
                                scope_code_id=scope_code_id,
                            ).delete()
                        )
            
            return Response({'study_scope_consents': StudyPatientScopeConsentSerializer(responses, many=True).data })


class FHIRPatientViewSet(ModelViewSet):
    
    serializer_class = FHIRBundledPatientSerializer
    pagination_class = FHIRBundlePagination

    def get_queryset(self):
        # GET /Patient?_has:Group:member:_id=<group-id>
        study_id = self.request.GET.get('_has:_group:member:_id', None)

        if not Study.practitioner_authorized(self.request.user.id, study_id):
            raise PermissionDenied("Current User does not have authorization to access this Study.")

        if not (study_id):
            raise BadRequest("Request parameter _has:Group:member:_id=<study_id> must be provided.")

        return Patient.fhir_search(self.request.user.id, study_id)