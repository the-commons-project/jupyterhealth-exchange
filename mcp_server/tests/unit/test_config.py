import pytest
from jhe_mcp.config import Settings


def test_settings_loads_required_env(monkeypatch):
    monkeypatch.setenv("JHE_BASE_URL", "http://localhost:8400")
    monkeypatch.setenv("JHE_CLIENT_ID", "test-client")
    settings = Settings.from_env()
    assert settings.jhe_base_url == "http://localhost:8400"
    assert settings.jhe_client_id == "test-client"
    assert settings.userinfo_endpoint == "http://localhost:8400/o/userinfo/"
    assert settings.token_endpoint == "http://localhost:8400/o/token/"
    assert settings.authorize_endpoint == "http://localhost:8400/o/authorize/"
    assert settings.redirect_uri == "http://localhost:8765/callback"


def test_settings_missing_required_env_raises(monkeypatch):
    monkeypatch.delenv("JHE_BASE_URL", raising=False)
    monkeypatch.setenv("JHE_CLIENT_ID", "test-client")
    with pytest.raises(RuntimeError, match="JHE_BASE_URL"):
        Settings.from_env()


def test_settings_overrides(monkeypatch):
    monkeypatch.setenv("JHE_BASE_URL", "https://jhe.example.com")
    monkeypatch.setenv("JHE_CLIENT_ID", "x")
    monkeypatch.setenv("JHE_REDIRECT_URI", "http://localhost:9999/cb")
    monkeypatch.setenv("JHE_USERINFO_ENDPOINT", "https://other.example.com/userinfo")
    settings = Settings.from_env()
    assert settings.redirect_uri == "http://localhost:9999/cb"
    assert settings.userinfo_endpoint == "https://other.example.com/userinfo"


def test_broker_fields_from_env(monkeypatch):
    monkeypatch.setenv("JHE_BASE_URL", "https://jhe.fly.dev")
    monkeypatch.setenv("JHE_CLIENT_ID", "abc")
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://jhe-mcp.fly.dev/")
    monkeypatch.setenv("MCP_BROKER_KEY", "super-secret-key")
    monkeypatch.setenv("MCP_ALLOWED_REDIRECTS", "https://a.test/cb, https://b.test/cb")
    from jhe_mcp.config import Settings

    s = Settings.from_env()
    assert s.mcp_resource_url == "https://jhe-mcp.fly.dev"  # trailing slash stripped
    assert s.broker_key == "super-secret-key"
    assert s.allowed_redirects == ("https://a.test/cb", "https://b.test/cb")


def test_broker_key_optional(monkeypatch):
    monkeypatch.setenv("JHE_BASE_URL", "https://jhe.fly.dev")
    monkeypatch.setenv("JHE_CLIENT_ID", "abc")
    monkeypatch.delenv("MCP_BROKER_KEY", raising=False)
    from jhe_mcp.config import Settings

    assert Settings.from_env().broker_key is None
