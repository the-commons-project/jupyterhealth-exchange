from allauth.account.adapter import DefaultAccountAdapter
from django.utils.translation import gettext_lazy as _


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
