from __future__ import annotations

import os
import time

import httpx
import pytest

JHE_URL = os.environ.get("MCP_TEST_JHE_URL", "http://localhost:8400")  # mcp-server worktree default
TEST_TOKEN = os.environ.get("MCP_TEST_TOKEN")


def _wait_for(url: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"JHE not reachable at {url}")


@pytest.fixture(scope="session")
def jhe_base_url() -> str:
    _wait_for(f"{JHE_URL}/health")
    return JHE_URL


@pytest.fixture(scope="session")
def test_token() -> str:
    if not TEST_TOKEN:
        pytest.skip("MCP_TEST_TOKEN env var not set; skipping integration tests")
    return TEST_TOKEN


@pytest.fixture(autouse=True)
def install_token(test_token: str):
    from jhe_mcp.auth.context import AuthContext, _current, set_current_auth

    token = set_current_auth(AuthContext(bearer_token=test_token, subject="test", expires_at=0))
    yield
    _current.reset(token)
