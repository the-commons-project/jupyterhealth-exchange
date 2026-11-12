"""
Unit tests for ``GET /health``, answered by ``core.middleware.HealthCheckMiddleware`` — run
locally with the Django test client (no ``--smoke-url`` required).  Live-deployment smoke
coverage lives in ``tests/smoke/test_smoke.py``.
"""

import pytest


@pytest.mark.django_db
class TestHealthViewUnit:
    """The liveness body — runs locally without ``--smoke-url``."""

    def test_health_returns_200_json(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_health_version_matches_settings(self, client):
        from django.conf import settings

        resp = client.get("/health")
        data = resp.json()
        assert data["version"] == settings.JHE_VERSION

    def test_health_unauthenticated(self, client):
        """``/health`` must work without any authentication."""
        # client fixture is unauthenticated by default
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_content_type(self, client):
        resp = client.get("/health")
        assert resp["Content-Type"] == "application/json"


PROBE_HOST = "10.0.1.23:8000"  # what a load balancer sends: the container's own address


@pytest.mark.django_db
class TestHealthCheckMiddleware:
    """A load balancer's probe carries the container's own IP as the Host header; ``/health``
    answers it anyway, and nothing else escapes the Host check."""

    def test_probe_with_disallowed_host_gets_the_liveness_body(self, client, settings):
        assert PROBE_HOST.split(":")[0] not in settings.ALLOWED_HOSTS
        resp = client.get("/health", HTTP_HOST=PROBE_HOST)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": settings.JHE_VERSION}

    def test_head_probe_is_answered_too(self, client):
        assert client.head("/health", HTTP_HOST=PROBE_HOST).status_code == 200

    def test_other_paths_keep_the_host_check(self, client):
        assert client.get("/", HTTP_HOST=PROBE_HOST).status_code == 400

    def test_other_methods_on_health_go_through_the_chain(self, client):
        assert client.post("/health", HTTP_HOST=PROBE_HOST).status_code == 400  # DisallowedHost
        assert client.post("/health").status_code == 404  # no view behind the path

    def test_health_is_not_a_route(self, client):
        from django.urls import NoReverseMatch, reverse

        with pytest.raises(NoReverseMatch):
            reverse("health")
