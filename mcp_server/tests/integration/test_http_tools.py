import os
import threading
import time

import httpx
import pytest
import uvicorn
from jhe_mcp.config import Settings
from jhe_mcp.server_http import build_app


@pytest.fixture(scope="module")
def http_server(jhe_base_url):
    """Start the FastAPI app in-process on port 18401."""
    os.environ["JHE_BASE_URL"] = jhe_base_url
    os.environ.setdefault("JHE_CLIENT_ID", "y-cAGY_FXAamPqFHRt5z_jX-W_DRvvka")
    settings = Settings.from_env()
    app = build_app(settings)

    config = uvicorn.Config(app, host="127.0.0.1", port=18401, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(30):
        try:
            r = httpx.get("http://127.0.0.1:18401/docs", timeout=1.0)
            if r.status_code in (200, 401, 404):
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    else:
        pytest.fail("HTTP server didn't start in 15 seconds")

    yield "http://127.0.0.1:18401"
    server.should_exit = True
    thread.join(timeout=5)


def test_unauthenticated_sse_rejected(http_server):
    r = httpx.get(f"{http_server}/sse", timeout=5.0)
    assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:200]}"


def test_invalid_token_sse_rejected(http_server):
    r = httpx.get(
        f"{http_server}/sse",
        headers={"Authorization": "Bearer not-a-real-jwt"},
        timeout=5.0,
    )
    assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:200]}"
