"""`manage.py bootstrap`: a migrated database becomes usable, nothing deployment-specific is
decided, and a rerun changes nothing an operator may have edited."""

import io

import pytest
from django.core.management import call_command
from oauth2_provider.models import get_application_model

from core import bootstrap
from core.models import (
    CodeableConcept,
    DataSource,
    EhrVendor,
    JheClient,
    JheSetting,
    JheUser,
    Organization,
)

Application = get_application_model()


def run():
    out = io.StringIO()
    call_command("bootstrap", stdout=out)
    return out.getvalue()


@pytest.mark.django_db
class TestFreshDatabase:
    def test_creates_the_reference_data_and_no_users(self):
        out = run()
        assert "Bootstrap complete" in out
        assert JheUser.objects.count() == 0
        assert CodeableConcept.objects.count() == len(bootstrap.CODEABLE_CONCEPTS)
        assert set(DataSource.objects.values_list("name", flat=True)) == {n for n, _, _ in bootstrap.DATA_SOURCES}
        assert Organization.objects.get(id=bootstrap.ROOT_ORGANIZATION_ID).type == "root"
        assert set(EhrVendor.objects.values_list("name", flat=True)) == set(bootstrap.EHR_VENDORS)
        assert all(v.ehr_client_id in (None, "") for v in EhrVendor.objects.all())

    def test_no_invite_code_is_set(self):
        """The row exists, empty, for an admin to fill in. (That an empty code closes
        registration is the form's behaviour, tested with it.)"""
        run()
        assert JheSetting.objects.get(key="site.registration_invite_code").get_value() == ""

    def test_admin_ui_application_for_the_browser_login(self, settings):
        settings.SITE_URL = "https://jhe.example.org"
        run()
        app = Application.objects.get(name=bootstrap.ADMIN_UI_APPLICATION)
        assert app.client_type == Application.CLIENT_PUBLIC
        assert app.authorization_grant_type == Application.GRANT_AUTHORIZATION_CODE
        assert app.redirect_uris == "https://jhe.example.org/auth/callback"
        assert app.skip_authorization is True
        assert app.client_id

    def test_clients_invite_to_this_site(self, settings):
        settings.SITE_URL = "https://jhe.example.org"
        run()
        for name, path in (
            ("USCDI Patient Access", "/clients/uscdi-patient-access/?code=CODE"),
            ("EHR Patient Portal", "/clients/ehr-patient-portal/?code=CODE"),
            ("Open Wearables", "/clients/ow/launch?code=CODE"),
        ):
            assert JheClient.objects.get(application__name=name).invitation_url == "https://jhe.example.org" + path

    def test_mcp_broker_only_with_credentials(self, monkeypatch):
        monkeypatch.delenv("MCP_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("MCP_OAUTH_CLIENT_SECRET", raising=False)
        out = run()
        assert "MCP_OAUTH_CLIENT_ID/SECRET not set" in out
        assert not Application.objects.filter(name=bootstrap.MCP_BROKER_APPLICATION).exists()
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", "broker-id")
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", "broker-secret")
        run()
        broker = Application.objects.get(name=bootstrap.MCP_BROKER_APPLICATION)
        assert broker.client_id == "broker-id" and broker.client_type == "confidential"


@pytest.mark.django_db
class TestRerun:
    def test_creates_nothing_and_keeps_edits(self):
        run()
        code = JheSetting.objects.get(key="site.registration_invite_code")
        code.set_value("string", "letmein")
        code.save()
        client = JheClient.objects.get(application__name="USCDI Patient Access")
        client.invitation_url = "https://else.example.org/x?code=CODE"
        client.save()
        before = {
            m: m.objects.count() for m in (JheSetting, CodeableConcept, DataSource, JheClient, Application, EhrVendor)
        }
        out = run()
        assert "created" not in out
        assert {m: m.objects.count() for m in before} == before
        assert JheSetting.objects.get(key="site.registration_invite_code").get_value() == "letmein"
        assert JheClient.objects.get(application__name="USCDI Patient Access").invitation_url.startswith(
            "https://else.example.org"
        )
        assert Application.objects.filter(name=bootstrap.ADMIN_UI_APPLICATION).count() == 1

    def test_seed_on_top_adds_the_demo_without_a_second_admin_ui_client(self):
        run()
        call_command("seed", stdout=io.StringIO())
        assert Application.objects.filter(name=bootstrap.ADMIN_UI_APPLICATION).count() == 1
        assert JheUser.objects.filter(email="manager_mary@example.com").exists()
        assert JheSetting.objects.get(key="site.registration_invite_code").get_value() == "jhe"
