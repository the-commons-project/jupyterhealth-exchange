from rest_framework import permissions
import logging


logger = logging.getLogger(__name__)

class IsSelfUrlPath(permissions.BasePermission):

    def has_permission(self, request, view):
        return int(request.parser_context['kwargs']['pk']) == request.user.id