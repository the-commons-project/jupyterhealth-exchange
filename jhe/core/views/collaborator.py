import logging
from rest_framework.viewsets import ModelViewSet
from core.serializers import StudyCollaboratorSerializer, StudyCollaboratorDetailSerializer
from core.models import StudyCollaborator, Study, JheUser
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from core.admin_pagination import AdminListMixin
from django.utils import timezone

logger = logging.getLogger(__name__)

class CollaboratorViewSet(ModelViewSet):
    model_class = StudyCollaborator
    serializer_class = StudyCollaboratorSerializer
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return StudyCollaboratorDetailSerializer
        return StudyCollaboratorSerializer
    
    def get_queryset(self):
        if self.detail:
            collaborator = get_object_or_404(StudyCollaborator, pk=self.kwargs['pk'])
            study = collaborator.study
            
            # Check if current user owns the study or is a collaborator themselves
            if self.user_can_manage_study(study.id):
                return StudyCollaborator.objects.filter(pk=self.kwargs['pk'])
            else:
                raise PermissionDenied("You don't have permission to view this collaborator.")
        else:
            study_id = self.request.query_params.get('study_id')
            if study_id:
                if self.user_can_manage_study(study_id):
                    return StudyCollaborator.objects.filter(study_id=study_id)
                else:
                    raise PermissionDenied("You don't have permission to view collaborators for this study.")
            else:
                # Return collaborators for all studies user can manage
                accessible_studies = Study.objects.accessible_by(self.request.user)
                return StudyCollaborator.objects.filter(study__in=accessible_studies)
    
    def user_can_manage_study(self, study_id):
        study = get_object_or_404(Study, pk=study_id)
        
        if Study.practitioner_authorized(self.request.user.id, study_id):
            return True
            
        # If user is a collaborator with management permissions (can be expanded later for RBAC)
        is_collaborator = StudyCollaborator.objects.filter(
            study_id=study_id, 
            jhe_user=self.request.user
        ).exists()
        
        return is_collaborator
    
    def create(self, request):
        study_id = request.data.get('study_id')
        user_email = request.data.get('email')
        
        if not study_id or not user_email:
            raise ValidationError("Both study_id and email are required")
            
        if not self.user_can_manage_study(study_id):
            raise PermissionDenied("You don't have permission to add collaborators to this study")
            
        try:
            user = JheUser.objects.get(email=user_email)
        except JheUser.DoesNotExist:
            raise ValidationError(f"No user found with email {user_email}")
            
        if StudyCollaborator.objects.filter(study_id=study_id, jhe_user=user).exists():
            raise ValidationError(f"User {user_email} is already a collaborator on this study")
            
        collaborator = StudyCollaborator.objects.create(
            study_id=study_id,
            jhe_user=user,
            granted_at=timezone.now()
        )
        
        return Response({
            'id': collaborator.id,
            'study_id': collaborator.study_id,
            'user_email': user.email,
            'granted_at': collaborator.granted_at
        })
    
    def destroy(self, request, pk=None):
        collaborator = self.get_object()
        
        if not self.user_can_manage_study(collaborator.study_id):
            raise PermissionDenied("You don't have permission to remove collaborators from this study")
            
        collaborator.delete()
        return Response({"status": "success", "message": "Collaborator removed successfully"})
    
    @action(detail=False, methods=['GET'])
    def for_study(self, request):
        """Get all collaborators for a specific study"""
        study_id = request.query_params.get('study_id')
        if not study_id:
            raise ValidationError("study_id parameter is required")
            
        if not self.user_can_manage_study(study_id):
            raise PermissionDenied("You don't have permission to view collaborators for this study")
            
        collaborators = StudyCollaborator.objects.filter(study_id=study_id)
        
        result = []
        for collab in collaborators:
            result.append({
                'id': collab.id,
                'study_id': collab.study_id,
                'user_id': collab.jhe_user_id,
                'user_email': collab.jhe_user.email,
                'granted_at': collab.granted_at
            })
            
        return Response(self.get_serializer(collaborators, many=True).data)
