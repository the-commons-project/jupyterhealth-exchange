import logging
from rest_framework.viewsets import ModelViewSet
from core.serializers import PatientSerializer, StudyDataSourceSerializer, StudyPatientSerializer, StudyScopeRequestSerializer, StudySerializer, StudyOrganizationSerializer
from core.models import Patient, Study, StudyDataSource, StudyPatient, StudyScopeRequest
from rest_framework.response import Response
from rest_framework.decorators import action
from django.core.exceptions import PermissionDenied, BadRequest

logger = logging.getLogger(__name__)

class StudyViewSet(ModelViewSet):

    def get_serializer_class(self):
        if self.request.method == 'GET': 
            return StudyOrganizationSerializer
        else:
            return StudySerializer

    def get_queryset(self):
        if self.detail:
            if Study.practitioner_authorized(self.request.user.id, self.kwargs['pk']):
                return Study.objects.filter(id=self.kwargs['pk'])
            else:
                raise PermissionDenied("Current User does not have authorization to access this Study.")
        else:
            return Study.for_practitioner_organization(self.request.user.id, self.request.GET.get('organization_id', None))

    @action(detail=True, methods=['GET','POST','DELETE'])
    def patients(self, request, pk):

        if request.method == 'GET': 
            patients = Patient.objects.raw(
            """
            SELECT core_patient.*, core_jheuser.email as telecom_email FROM core_patient
            JOIN core_jheuser ON core_patient.jhe_user_id=core_jheuser.id
            JOIN core_studypatient ON core_patient.id=core_studypatient.patient_id
            WHERE core_studypatient.study_id={study_id}
            """.format(study_id=pk))

            serializer = PatientSerializer(patients, many=True)
            return Response(serializer.data)
        else:
            responses = []
            for patient_id in request.data['patient_ids']:
                if request.method == 'POST':
                    study = Study.objects.get(id=pk)
                    patient = Patient.objects.get(id=patient_id)
                    if study.organization_id != patient.organization_id:
                        raise BadRequest('Patient and study must be from the same Organization')
                    responses.append(
                        StudyPatient.objects.create(study_id=pk, patient_id=patient_id)
                    )
                else:
                    responses.append(
                        StudyPatient.objects.filter(study_id=pk, patient_id=patient_id).delete()
                    )
        
            return Response({'study_patients': StudyPatientSerializer(responses, many=True).data })


    @action(detail=True, methods=['GET','POST','DELETE'])
    def scope_requests(self, request, pk):

        if request.method == 'GET': 
            scopes = StudyScopeRequest.objects.filter(study_id=pk).order_by('id')
            serializer = StudyScopeRequestSerializer(scopes, many=True)
            return Response(serializer.data)
        else:
            response = None
            if request.method == 'POST':
                response = StudyScopeRequest.objects.create(study_id=pk, scope_code_id=request.data["scope_code_id"])
            else:
                response = StudyScopeRequest.objects.filter(study_id=pk, scope_code_id=request.data["scope_code_id"]).delete()
        
            return Response(StudyScopeRequestSerializer(response, many=False).data)
        
    @action(detail=True, methods=['GET','POST','DELETE'])
    def data_sources(self, request, pk):

        if request.method == 'GET': 
            study_data_sources = StudyDataSource.objects.filter(study_id=pk).order_by('id')
            serializer = StudyDataSourceSerializer(study_data_sources, many=True)
            return Response(serializer.data)
        else:
            response = None
            if request.method == 'POST':
                response = StudyDataSource.objects.create(study_id=pk, data_source_id=request.data["data_source_id"])
            else:
                response = StudyDataSource.objects.filter(study_id=pk, data_source_id=request.data["data_source_id"]).delete()
        
            return Response(StudyDataSourceSerializer(response, many=False).data)
