from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.utils.translation import gettext_lazy as _

from core.services.jhe_settings import get_setting


class JheAccountAdapter(DefaultAccountAdapter):
    """Rewords allauth's bundled account error messages so they read consistently.

    allauth raises these via `adapter.validation_error(code)`, which looks the
    text up in `error_messages[code]`. We only override the keys surfaced by the
    email one-time-code login; everything else falls back to allauth's defaults
    through the dict merge. Add more keys here (see
    allauth.account.adapter.DefaultAccountAdapter.error_messages) to reword others.
    """

    error_messages = {
        **DefaultAccountAdapter.error_messages,
        "incorrect_code": _("That sign-in code is incorrect. Please try again."),
        "too_many_login_attempts": _("Too many incorrect attempts. Please request a new code and try again."),
        "unknown_email": _("We couldn't find an account for that email address."),
        "rate_limited": _("You're making requests too quickly. Please wait a moment and try again."),
    }


class JheSocialAccountAdapter(DefaultSocialAccountAdapter):
    """SAML SSO glue: IdP-asserted users are provisioned as practitioners,
    optionally restricted to email domains in the `auth.sso.valid_domains`
    JheSetting (comma-separated; empty allows any). The gate applies to
    first-time provisioning only — an already-linked SocialAccount keeps
    logging in if the setting is tightened later; deactivate the user to
    revoke access. Rejected signups get allauth's "Sign Up Closed" page.
    """

    def is_open_for_signup(self, request, sociallogin):
        email = (sociallogin.user.email or "").lower()
        if not email:
            # No email asserted (misconfigured IdP attribute mapping): fail
            # closed rather than fall through to allauth's type-any-email
            # signup form, which would mint an unverified practitioner.
            return False
        valid_domains = get_setting("auth.sso.valid_domains", "")
        if not valid_domains:
            return True
        allowed = {d.strip().lstrip("@").lower() for d in valid_domains.split(",") if d.strip()}
        return email.rsplit("@", 1)[-1] in allowed

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        user.user_type = "practitioner"
        user.identifier = sociallogin.account.uid
        return user
