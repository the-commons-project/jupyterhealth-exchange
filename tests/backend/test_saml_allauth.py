# Tests for the allauth-based SAML SSO wiring that replaced django_saml2_auth
# (RFC 0002 §5). The IdP itself is a SocialApp row; these tests cover the JHE
# glue: the login-page button gate and the practitioner-provisioning adapter.
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import JheSetting, JheUser
from core.user_messages import JheSocialAccountAdapter


def _set_setting(key, value_type, value):
    setting, _ = JheSetting.objects.get_or_create(key=key, defaults={"value_type": value_type})
    setting.set_value(value_type, value)
    setting.save()


class SamlUrlsTests(TestCase):
    """allauth's saml provider registers the SSO endpoints under /allauth/."""

    def test_saml_endpoints_route(self):
        self.assertEqual(reverse("saml_login", kwargs={"organization_slug": "idp"}), "/allauth/saml/idp/login/")
        self.assertEqual(reverse("saml_acs", kwargs={"organization_slug": "idp"}), "/allauth/saml/idp/acs/")
        self.assertEqual(reverse("saml_metadata", kwargs={"organization_slug": "idp"}), "/allauth/saml/idp/metadata/")


class LoginPageSamlButtonTests(TestCase):
    """The SAML button stays gated by the auth.sso.saml2 JheSetting."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_button_hidden_when_disabled(self):
        _set_setting("auth.sso.saml2", "int", 0)
        response = self.client.get("/accounts/login/")
        self.assertNotContains(response, "Continue with SAML2")

    def test_button_links_to_allauth_when_enabled(self):
        SocialApp.objects.create(provider="saml", name="Test IdP", client_id="idp")
        _set_setting("auth.sso.saml2", "int", 1)
        response = self.client.get("/accounts/login/")
        self.assertContains(response, "Continue with SAML2")
        self.assertContains(response, "/allauth/saml/idp/login/")


class JheSocialAccountAdapterTests(TestCase):
    """SAML logins provision practitioners, optionally domain-restricted."""

    def setUp(self):
        cache.clear()
        self.adapter = JheSocialAccountAdapter()
        self.request = RequestFactory().get("/")

    def tearDown(self):
        cache.clear()

    def _sociallogin(self, email, uid="idp-uid-1"):
        user = JheUser(email=email)
        return SocialLogin(user=user, account=SocialAccount(provider="saml", uid=uid))

    def test_populate_user_provisions_practitioner(self):
        sociallogin = self._sociallogin("doc@example.org")
        user = self.adapter.populate_user(self.request, sociallogin, {"email": "doc@example.org"})
        self.assertEqual(user.user_type, "practitioner")
        self.assertEqual(user.identifier, "idp-uid-1")

    def test_signup_open_without_domain_restriction(self):
        self.assertTrue(self.adapter.is_open_for_signup(self.request, self._sociallogin("doc@anywhere.net")))

    def test_signup_respects_valid_domains(self):
        _set_setting("auth.sso.valid_domains", "string", "example.org, hospital.edu")
        self.assertTrue(self.adapter.is_open_for_signup(self.request, self._sociallogin("doc@EXAMPLE.org")))
        self.assertFalse(self.adapter.is_open_for_signup(self.request, self._sociallogin("doc@evil.com")))
