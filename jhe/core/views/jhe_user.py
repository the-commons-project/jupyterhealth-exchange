import logging
from core.models import JheUser, Organization
from core.serializers import JheUserSerializer, OrganizationSerializer
from core.permissions import IsSelfUrlPath
from rest_framework.viewsets import ModelViewSet
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.admin_pagination import CustomPageNumberPagination

logger = logging.getLogger(__name__)

class JheUserViewSet(ModelViewSet):

    serializer_class = JheUserSerializer
    pagination_class = CustomPageNumberPagination
    model_class = JheUser

    def get_permissions(self):
        permission_classes = [IsAuthenticated]
        if self.action in ['retrieve']:
            permission_classes.append(IsSelfUrlPath)
        # to switch off permissions for dev:
        # permission_classes = []
        return [permission() for permission in permission_classes]

    def get_queryset(self):
        param_email = self.request.query_params.get('email')
        if param_email:
            return JheUser.objects.filter(email=param_email).order_by('last_name')
        else:
            return JheUser.objects.order_by('last_name')
        
    @action(detail=False, methods=['GET'])
    def profile(self, request):
        user_with_patient = request.user
        user_with_patient.patient = request.user.get_patient
        jhe_user_serializer = JheUserSerializer(request.user, many=False)
        return Response(jhe_user_serializer.data)
    
    @action(detail=False, methods=['GET'])
    def organizations(self, request):
        organizations = Organization.for_practitioner(request.user.id)
        organization_serializer = OrganizationSerializer(organizations, many=True)
        return Response(organization_serializer.data)

    


    