import logging
from core.models import Organization, PractitionerOrganization, PatientOrganization
from core.serializers import (
  OrganizationSerializer, OrganizationUsersSerializer, StudySerializer, PractitionerOrganizationSerializer,
  PatientOrganizationSerializer
)
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from core.admin_pagination import CustomPageNumberPagination
from core.models import JheUser
from django.shortcuts import get_object_or_404

logger = logging.getLogger(__name__)

class OrganizationViewSet(ModelViewSet):
    """
    This viewset automatically provides `list`, `create`, `retrieve`,
    `update` and `destroy` actions.
    """
    serializer_class = OrganizationSerializer
    model_class = Organization
    pagination_class = CustomPageNumberPagination

    def get_queryset(self):
        param_part_of = self.request.query_params.get('part_of')
        if param_part_of:
            return Organization.objects.filter(part_of=param_part_of).order_by('name')
        else:
            return Organization.objects.order_by('name')
    
    @action(detail=False, methods=['GET'])
    def types(self, request):
        return Response(Organization.ORGANIZATION_TYPE_CHOICES)
    
    @action(detail=True, methods=['GET'])
    def tree(self, request, pk):
        parent = self.get_object()
        Organization.collect_children(parent)
        return Response(self.get_serializer(parent).data)
    
    def collect_children(self, parent):
        children = self.get_children(parent.id)
        for child in children:
            parent.children.append(child)
            self.collect_children(child)

    def get_children(self, parent_id):
        return Organization.objects.filter(part_of=parent_id).order_by('name')
    
    @action(detail=True, methods=['GET'])
    def users(self, request, pk):
        organization = self.get_object()
        users = organization.users.order_by('last_name')
        serializer = OrganizationUsersSerializer(users, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['POST','DELETE'])
    def user(self, request, pk):
      user_type = request.data.get('user_type', 'practitioner')  # Default to practitioner if not specified
      jhe_user_id = request.data.get('jhe_user_id')
      
      if not jhe_user_id:
        return Response({"error": "jhe_user_id is required"}, status=400)
      
      jhe_user = get_object_or_404(JheUser, pk=jhe_user_id)
      
      if user_type.lower() == 'patient':
        if request.method == 'POST':
          relation = PatientOrganization.objects.create(
            organization_id=pk, 
            patient_id=jhe_user_id
          )
          serializer = PatientOrganizationSerializer(relation)
        else:
          relation = PatientOrganization.objects.filter(
            organization_id=pk, 
            patient_id=jhe_user_id
          ).delete()
          return Response(status=204)
      else:  # practitioner
        practitioner = jhe_user.practitioner
        if not practitioner:
            return Response({"error": "Practitioner not found"}, status=404)
        if request.method == 'POST':
          relation = PractitionerOrganization.objects.create(
            organization_id=pk, 
            practitioner=practitioner
          )
          serializer = PractitionerOrganizationSerializer(relation)
        else:
          relation = PractitionerOrganization.objects.filter(
            organization_id=pk, 
            practitioner=practitioner
          ).delete()
          return Response(status=204)
      
      return Response(serializer.data)

    @action(detail=True, methods=['GET'])
    def studies(self, request, pk):
        organization = self.get_object()
        studies = organization.study_set.order_by('name')
        serializer = StudySerializer(studies, many=True)
        return Response(serializer.data)

