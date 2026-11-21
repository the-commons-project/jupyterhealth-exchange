from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.core.exceptions import SignupClosedException
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.services.jhe_settings import get_setting

SSO_REQUIRED_MESSAGE = _('Practitioners sign in with SAML2. Use the "Continue with SAML2" button.')


def sso_required_for(user):
    """True when SSO-only is active and `user` is a practitioner, staff or superuser, who may then
    sign in only through the SAML IdP. Patients are never affected."""
    from core.context_processors import sso_only_active

    return bool(
        sso_only_active() and (user.is_staff or user.is_superuser or (user.is_practitioner() and not user.is_patient()))
    )


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

    def pre_login(self, request, user, *, signal_kwargs, **kwargs):
        """The email-code login (allauth) refuses practitioners when SSO is required. An SSO login
        carries a `sociallogin` in `signal_kwargs`, which is how it is told apart. The password form
        is Django's LoginView (core.views.common), which checks the same rule; the Django admin's
        own login (/admin/login/) is limited by `auth.admin.allowed_cidrs` instead."""
        if not (signal_kwargs or {}).get("sociallogin") and sso_required_for(user):
            messages.error(request, SSO_REQUIRED_MESSAGE)
            return HttpResponseRedirect(reverse("login"))
        return super().pre_login(request, user, signal_kwargs=signal_kwargs, **kwargs)


class JheSocialAccountAdapter(DefaultSocialAccountAdapter):
    """SAML SSO glue: IdP-asserted users are provisioned as practitioners,
    optionally restricted to email domains in the `auth.sso.valid_domains`
    JheSetting (comma-separated; empty allows any). The domain gate applies to
    first-time provisioning only — existing accounts reached via
    email-authentication, and already-linked SocialAccounts, never pass through
    it; deactivate the user to revoke access. `user_type` is likewise set at
    first-time provisioning only. Rejections render allauth's "Sign Up Closed"
    page.
    """

    def pre_social_login(self, request, sociallogin):
        # SAML SSO is a practitioner entrance: don't let an existing patient
        # account (same IdP-asserted email, or previously linked) into a
        # practitioner portal session.
        if sociallogin.is_existing and sociallogin.user.is_patient():
            raise SignupClosedException()
        # When allauth matched this login to an existing user by verified
        # IdP-asserted email (_did_authenticate_by_email, set in lookup()),
        # its anti-pre-registration guard would wipe the user's password in
        # _accept_login unless a *verified* allauth EmailAddress row exists —
        # and JHE practitioners created via our own signup/admin paths have
        # none. Backfill one: the IdP's email trust (the same decision that
        # enabled email-authentication for this SocialApp) vouches for it.
        email = getattr(sociallogin, "_did_authenticate_by_email", None)
        if email and sociallogin.user.pk:
            address, _ = EmailAddress.objects.get_or_create(
                user=sociallogin.user,
                email=email.lower(),
                defaults={"verified": True, "primary": True},
            )
            if not address.verified:
                address.verified = True
                address.save(update_fields=["verified"])
            # Keep JHE's own flag in step with the verified allauth row. Without
            # this, an email-linked practitioner reaches login through here (not
            # populate_user, which only runs for brand-new SSO users) and would
            # linger as unverified in the JHE admin panel.
            if not sociallogin.user.email_is_verified:
                sociallogin.user.email_is_verified = True
                sociallogin.user.save(update_fields=["email_is_verified"])

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
        # rsplit, not partition: the domain is what follows the LAST "@" (RFC
        # 5321 quoted local parts may contain "@"), matching Django's
        # EmailValidator parse.
        return email.rsplit("@", 1)[-1] in allowed

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        user.user_type = "practitioner"
        # Mirrors the verified allauth EmailAddress row this login records —
        # the IdP asserted the email, so JHE's own flag agrees with it.
        user.email_is_verified = True
        return user
