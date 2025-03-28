import logging
from core.utils import FHIRBundlePagination
from core.views.fhir_base import FHIRBase
from rest_framework import status
from rest_framework.viewsets import ModelViewSet
from core.serializers import FHIRBundledObservationSerializer, FHIRObservationSerializer, ObservationSerializer
from core.models import Observation, Patient, Study
from rest_framework.response import Response
from django.core.exceptions import PermissionDenied, BadRequest
import math

logger = logging.getLogger(__name__)

class ObservationViewSet(ModelViewSet):
    serializer_class = ObservationSerializer
    pagination_class = FHIRBundlePagination

    def get_queryset(self):
        """Return queryset for list view - this must return a queryset or list, not a Response"""
        """Specifically use queryset and operate on it directly, instead of converting to list which dumps the queryset into memory"""
        if self.detail:
            if Observation.practitioner_authorized(self.request.user.id, self.kwargs['pk']):
                return Patient.objects.filter(id=self.kwargs['pk'])
            else:
                raise PermissionDenied("Current User does not have authorization to access this Observation.")
        else:
            organization_id = self.request.query_params.get('organization_id')
            study_id = self.request.query_params.get('study_id')
            patient_id = self.request.query_params.get('patient_id')
            
            return Observation.objects.filter(
              patient__practitioner_users=self.request.user.id,
              **({"organization_id": organization_id} if organization_id else {}),
              **({"study_id": study_id} if study_id else {}),
              **({"patient_id": patient_id} if patient_id else {})
            )
    
    def list(self, request, *args, **kwargs):
        """Override list method to handle raw SQL pagination"""
        page_size = self.pagination_class().get_page_size(request)
        page_number = self.pagination_class().get_page_number(request)
        offset = (page_number - 1) * page_size
        
        # TBD: Do we have a need to support both camelCase and snake_case?
        organization_id = request.query_params.get('organizationId') or request.query_params.get('organization_id')
        study_id = request.query_params.get('studyId') or request.query_params.get('study_id')
        patient_id = request.query_params.get('patientId') or request.query_params.get('patient_id')
        
        total_count = Observation.count_for_practitioner_organization_study_patient(
            practitioner_user_id=request.user.id,
            organization_id=organization_id,
            study_id=study_id,
            patient_id=patient_id
        )
        
        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 1
        
        observations = Observation.for_practitioner_organization_study_patient(
            practitioner_user_id=request.user.id,
            organization_id=organization_id,
            study_id=study_id,
            patient_id=patient_id,
            offset=offset,
            page=page_size
        )
        
        serializer = self.get_serializer(observations, many=True)
        
        # Build FHIR response with proper total and links
        response = {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': total_count,
            'entry': serializer.data,
            'link': [],
            # TBD: we can remove this if we want to keep it strictly FHIR
            'meta': {
                'pagination': {
                    'page': page_number,
                    'pageSize': page_size,
                    'totalPages': total_pages,
                }
            }
        }
        
        # Always include a self link
        response['link'].append({
            'relation': 'self',
            'url': request.build_absolute_uri()
        })
        
        # Previous page link
        if page_number > 1:
            prev_params = request.query_params.copy()
            prev_params['_page'] = page_number - 1
            prev_url = f"{request.build_absolute_uri().split('?')[0]}?{prev_params.urlencode()}"
            response['link'].append({
                'relation': 'previous',
                'url': prev_url
            })
        
        # Next page link
        if page_number < total_pages:
            next_params = request.query_params.copy()  # Keep all original parameters with original names
            next_params['_page'] = page_number + 1     # Update only the page number
            next_url = f"{request.build_absolute_uri().split('?')[0]}?{next_params.urlencode()}"
            response['link'].append({
                'relation': 'next',
                'url': next_url
            })
        
        # First page link
        if page_number > 1:
            first_params = request.query_params.copy()
            first_params['_page'] = 1
            first_url = f"{request.build_absolute_uri().split('?')[0]}?{first_params.urlencode()}"
            response['link'].append({
                'relation': 'first',
                'url': first_url
            })
        
        # Last page link
        if page_number < total_pages:
            last_params = request.query_params.copy()
            last_params['_page'] = total_pages
            last_url = f"{request.build_absolute_uri().split('?')[0]}?{last_params.urlencode()}"
            response['link'].append({
                'relation': 'last',
                'url': last_url
            })
        print(f"response: {response}")
        return Response(response)


class FHIRObservationViewSet(ModelViewSet):
    
    pagination_class = FHIRBundlePagination

    def get_serializer_class(self):
        if self.request.method == 'GET': 
            return FHIRBundledObservationSerializer
        else:
            return FHIRObservationSerializer

    
    def get_queryset(self):
        # GET /Observation?patient._has:Group:member:_id=<group-id>
        study_id = self.request.GET.get('patient._has:_group:member:_id', None)
        if study_id is None: # TBD: remove this
            study_id = self.request.GET.get('_has:_group:member:_id', None)
        
        patient_id = self.request.GET.get('patient', None)
        coding_system_and_value = self.request.GET.get('code', None)

        if not (study_id or patient_id):
            raise BadRequest("Request parameter patient._has:Group:member:_id=<study_id> or patient=<patient_id> must be provided.")
        
        if study_id and (not Study.practitioner_authorized(self.request.user.id, study_id)):
            raise PermissionDenied("Current User does not have authorization to access this Study.")

        if study_id and patient_id and (not Study.has_patient(study_id, patient_id)):
            raise BadRequest("The requested Patient is not part of the specified Study.")

        coding_system = None
        coding_value = None
        if coding_system_and_value:
            coding_split = coding_system_and_value.split('|')
            coding_system = coding_split[0]
            coding_value = coding_split[1]

        return Observation.fhir_search(
            self.request.user.id,
            study_id,
            patient_id,
            coding_system,
            coding_value
        )
    
    def create(self, request):
        observation = None
        try:
            observation = Observation.fhir_create(request.data, request.user)
        # TBD: except PermissionDenied:
        except Exception as e:
            return Response(FHIRBase.error_outcome(str(e)), status=status.HTTP_400_BAD_REQUEST)
        
        fhir_observation = Observation.fhir_search(self.request.user.id, None, None, None, None, None, observation.id)[0]
        serializer = FHIRObservationSerializer(fhir_observation, many=False)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
