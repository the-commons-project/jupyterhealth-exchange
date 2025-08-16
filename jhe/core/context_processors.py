import json
from django.conf import settings
from core.models import DataSource, Organization


def constants(request):

    return {
        "JHE_VERSION": settings.JHE_VERSION,
        "SITE_TITLE": settings.SITE_TITLE,
        "SITE_URL": settings.SITE_URL,
        "OIDC_CLIENT_AUTHORITY": settings.OIDC_CLIENT_AUTHORITY,
        "OIDC_CLIENT_ID": settings.OIDC_CLIENT_ID,
        "OIDC_CLIENT_REDIRECT_URI": settings.OIDC_CLIENT_REDIRECT_URI,
        "SAML2_ENABLED": settings.SAML2_ENABLED,
        "ORGANIZATION_TYPES": json.dumps(Organization.ORGANIZATION_TYPE_CHOICES),
        "DATA_SOURCE_TYPES": json.dumps(DataSource.DATA_SOURCE_TYPE_CHOICES),
    }
