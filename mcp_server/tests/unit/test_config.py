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
