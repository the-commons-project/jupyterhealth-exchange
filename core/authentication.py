from oauth2_provider.contrib.rest_framework import OAuth2Authentication


class JheOAuth2Authentication(OAuth2Authentication):
    """Resolve a user for client-credentials tokens too.

    Authorization-code (and password/refresh) tokens carry the resource owner directly on the
    token, so DRF exposes it as ``request.user``. Client-credentials tokens have no resource
    owner -- django-oauth-toolkit saves them with ``user=None`` -- so DRF would otherwise treat
    the request as unauthenticated. For those, fall back to the application owner
    (``token.application.user``); for a PractitionerClient that is the practitioner who created
    it. This keeps ``request.user`` correct everywhere without per-view changes.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        if user is None and token is not None:
            application = getattr(token, "application", None)
            if application is not None and application.user is not None:
                user = application.user
        return user, token
