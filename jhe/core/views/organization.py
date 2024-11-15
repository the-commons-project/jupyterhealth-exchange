import logging
from core.models import Organization, JheUserOrganization
from core.serializers import JheUserOrganizationSerializer, OrganizationSerializer, OrganizationUsersSerializer, StudySerializer
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

logger = logging.getLogger(__name__)

class OrganizationViewSet(ModelViewSet):
    """
    This viewset automatically provides `list`, `create`, `retrieve`,
    `update` and `destroy` actions.
    """
    serializer_class = OrganizationSerializer

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
        response = None
        if request.method == 'POST':
            response = JheUserOrganization.objects.create(organization_id=pk, jhe_user_id=request.data['jhe_user_id'])
        else:
            response = JheUserOrganization.objects.filter(organization_id=pk, jhe_user_id=request.data['jhe_user_id']).delete()
        serializer = JheUserOrganizationSerializer(response, many=False)
        return Response(serializer.data)

    @action(detail=True, methods=['GET'])
    def studies(self, request, pk):
        organization = self.get_object()
        studies = organization.study_set.order_by('name')
        serializer = StudySerializer(studies, many=True)
        return Response(serializer.data)

