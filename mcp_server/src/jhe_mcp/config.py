from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    jhe_base_url: str
    jhe_client_id: str
    jhe_client_secret: str | None
    redirect_uri: str
    authorize_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str

    @classmethod
    def from_env(cls) -> Settings:
        base = os.environ.get("JHE_BASE_URL")
        if not base:
            raise RuntimeError("JHE_BASE_URL is required")
        client_id = os.environ.get("JHE_CLIENT_ID")
        if not client_id:
            raise RuntimeError("JHE_CLIENT_ID is required")
        base = base.rstrip("/")
        return cls(
            jhe_base_url=base,
            jhe_client_id=client_id,
            jhe_client_secret=os.environ.get("JHE_CLIENT_SECRET"),
            redirect_uri=os.environ.get("JHE_REDIRECT_URI", "http://localhost:8765/callback"),
            authorize_endpoint=os.environ.get("JHE_AUTHORIZE_ENDPOINT", f"{base}/o/authorize/"),
            token_endpoint=os.environ.get("JHE_TOKEN_ENDPOINT", f"{base}/o/token/"),
            userinfo_endpoint=os.environ.get("JHE_USERINFO_ENDPOINT", f"{base}/o/userinfo/"),
        )
