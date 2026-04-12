"""Custom OAuth2 authentication that supports client_credentials service accounts."""

from oauth2_provider.contrib.rest_framework import OAuth2Authentication


class ServiceAccountOAuth2Authentication(OAuth2Authentication):
    """Extends OAuth2Authentication to support client_credentials tokens.

    For client_credentials tokens (no user on the token), falls back to the
    application's owner user. This allows service accounts like the Open
    Wearables push client to authenticate as a system user.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result

        # If no user on the token (client_credentials), use the app's user
        if user is None or (hasattr(user, 'is_anonymous') and user.is_anonymous):
            if token and hasattr(token, 'application') and token.application and token.application.user:
                user = token.application.user

        return (user, token)
