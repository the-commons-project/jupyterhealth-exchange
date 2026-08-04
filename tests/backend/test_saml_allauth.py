"""JHE glue around allauth's SAML provider: the login-button gate and the
practitioner-provisioning adapter (see docs/rfcs/0003)."""

from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import JheSetting, JheUser, Practitioner
from core.user_messages import JheSocialAccountAdapter

# Minimal-but-valid SocialApp settings: the idp block is mandatory for
# build_saml_config, and allow_single_label_domains lets python3-saml's strict
# mode accept the test client's "testserver" host.
IDP_SETTINGS = {
    "idp": {"entity_id": "https://idp.example.org/metadata", "sso_url": "https://idp.example.org/sso", "x509cert": ""},
    "advanced": {"allow_single_label_domains": True},
}


def _set_setting(key, value_type, value):
    setting, _ = JheSetting.objects.update_or_create(key=key, defaults={"value_type": value_type})
    setting.set_value(value_type, value)
    setting.save()


class SamlUrlsTests(TestCase):
    """The SSO endpoints are the SP's external contract — IdPs register them."""

    def test_saml_endpoints_route(self):
        self.assertEqual(reverse("saml_login", kwargs={"organization_slug": "idp"}), "/allauth/saml/idp/login/")
        self.assertEqual(reverse("saml_acs", kwargs={"organization_slug": "idp"}), "/allauth/saml/idp/acs/")
        self.assertEqual(reverse("saml_metadata", kwargs={"organization_slug": "idp"}), "/allauth/saml/idp/metadata/")

    def test_metadata_endpoint_serves_sp_descriptor(self):
        """Exercises SocialApp JSON -> build_saml_config -> python3-saml/xmlsec."""
        SocialApp.objects.create(provider="saml", name="Test IdP", client_id="idp", settings=IDP_SETTINGS)
        response = self.client.get("/allauth/saml/idp/metadata/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("xml", response["Content-Type"])
        self.assertIn(b"EntityDescriptor", response.content)
        self.assertIn(b"/allauth/saml/idp/acs/", response.content)


class LoginPageSamlButtonTests(TestCase):
    """The SAML button requires the auth.sso.saml2 flag AND exactly one
    SocialApp — with zero or several, provider_login_url would 500 the whole
    login page, so the button degrades to hidden instead."""

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

    def test_button_hidden_when_enabled_without_social_app(self):
        _set_setting("auth.sso.saml2", "int", 1)
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Continue with SAML2")

    def test_button_hidden_when_multiple_social_apps(self):
        SocialApp.objects.create(provider="saml", name="IdP A", client_id="a")
        SocialApp.objects.create(provider="saml", name="IdP B", client_id="b")
        _set_setting("auth.sso.saml2", "int", 1)
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Continue with SAML2")


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

    def test_saved_signup_creates_practitioner_profile(self):
        """The composition the adapter relies on: save_user persists the user
        and JheUser.save() auto-creates the Practitioner profile."""
        SessionMiddleware(lambda r: None).process_request(self.request)
        sociallogin = self._sociallogin("newdoc@example.org")
        sociallogin.user = self.adapter.populate_user(self.request, sociallogin, {"email": "newdoc@example.org"})
        user = self.adapter.save_user(self.request, sociallogin, form=None)
        self.assertTrue(Practitioner.objects.filter(jhe_user=user).exists())

    def test_signup_open_without_domain_restriction(self):
        self.assertTrue(self.adapter.is_open_for_signup(self.request, self._sociallogin("doc@anywhere.net")))

    def test_signup_closed_without_email(self):
        """Misconfigured IdP attribute mapping must not fall through to the
        type-any-email signup form."""
        self.assertFalse(self.adapter.is_open_for_signup(self.request, self._sociallogin("")))

    def test_signup_respects_valid_domains(self):
        _set_setting("auth.sso.valid_domains", "string", "example.org, @hospital.edu")
        self.assertTrue(self.adapter.is_open_for_signup(self.request, self._sociallogin("doc@EXAMPLE.org")))
        self.assertTrue(self.adapter.is_open_for_signup(self.request, self._sociallogin("doc@hospital.edu")))
        self.assertFalse(self.adapter.is_open_for_signup(self.request, self._sociallogin("doc@evil.com")))
