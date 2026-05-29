from __future__ import annotations

import json
import urllib.parse
from collections.abc import Awaitable, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from jhe_mcp.auth.oauth_flow import AuthenticationRequired
from jhe_mcp.config import Settings
from jhe_mcp.omh_registry import all_schema_ids, all_short_names, load_schema, short_name
from jhe_mcp.tools import study as study_tools

AUTH_REQUIRED_MSG = (
    "Authentication required. Open this URL in your browser to log in:\n\n"
    "{url}\n\n"
    "After logging in, retry your request."
)


def build_server(
    settings: Settings,
    pre_tool_hook: Callable[[], Awaitable[None]] | None = None,
) -> FastMCP:
    parsed = urllib.parse.urlparse(settings.mcp_resource_url)
    public_host = parsed.netloc  # host[:port]
    public_origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed_hosts = [public_host, f"{public_host}:*", "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    allowed_origins = [public_origin, "http://localhost:*", "http://127.0.0.1:*"]
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    mcp = FastMCP(name="jhe-mcp", transport_security=transport_security)
    base_url = settings.jhe_base_url

    async def _before() -> str | None:
        if pre_tool_hook is not None:
            try:
                await pre_tool_hook()
            except AuthenticationRequired as exc:
                return AUTH_REQUIRED_MSG.format(url=exc.url)
        return None

    def _make_schema_reader(sid: str):
        async def _read() -> str:
            return json.dumps(load_schema(sid), indent=2)

        return _read

    for schema_id in sorted(all_schema_ids()):
        sname = short_name(schema_id)
        uri = f"omh://schema/{sname}"
        try:
            schema = load_schema(schema_id)
            description = schema.get("description", f"OMH schema: {schema_id}")
            reader = _make_schema_reader(schema_id)
            reader.__doc__ = description
            reader.__name__ = f"omh_schema_{sname.replace('-', '_')}"
            mcp.resource(uri)(reader)
        except (KeyError, FileNotFoundError):
            pass

    @mcp.tool()
    async def get_omh_schema(name: str) -> dict | str:
        """Return the full OMH JSON schema for a data type short name.

        Known names: blood-glucose, blood-pressure, body-temperature,
        heart-rate, heart-rate-variability, oxygen-saturation,
        physical-activity, respiratory-rate, rr-interval,
        sleep-duration, sleep-episode, step-count.
        """
        if auth_msg := await _before():
            return auth_msg
        for sid in all_schema_ids():
            if short_name(sid) == name:
                return load_schema(sid)
        return {"error": f"Unknown schema name {name!r}", "known": all_short_names()}

    @mcp.tool()
    async def get_study_count() -> int | str:
        """How many studies the authenticated user can see."""
        if auth_msg := await _before():
            return auth_msg
        return await study_tools.get_study_count(base_url=base_url)

    @mcp.tool()
    async def list_studies() -> list[dict] | str:
        """List studies the authenticated user can see."""
        if auth_msg := await _before():
            return auth_msg
        studies = await study_tools.list_studies(base_url=base_url)
        return [s.model_dump() for s in studies]

    @mcp.tool()
    async def get_study_metadata(study_id: str) -> dict | str | None:
        """Metadata for one study by ID. Returns null if not found or not authorized."""
        if auth_msg := await _before():
            return auth_msg
        meta = await study_tools.get_study_metadata(study_id=study_id, base_url=base_url)
        return meta.model_dump() if meta else None

    @mcp.tool()
    async def list_study_patients(study_id: str) -> list[dict] | str:
        """Patients enrolled in a study — returns ID, name, and email for each."""
        if auth_msg := await _before():
            return auth_msg
        patients = await study_tools.list_study_patients(study_id=study_id, base_url=base_url)
        return [p.model_dump() for p in patients]

    @mcp.tool()
    async def get_patient_demographics(patient_id: str) -> dict | str | None:
        """Slim demographics for a patient by ID."""
        if auth_msg := await _before():
            return auth_msg
        d = await study_tools.get_patient_demographics(patient_id=patient_id, base_url=base_url)
        return d.model_dump() if d else None

    @mcp.tool()
    async def get_patient_observations(
        patient_id: str,
        data_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict] | str:
        """
        Observations for a patient, optionally filtered by OMH data type short
        name (e.g., 'blood-glucose', 'heart-rate') and ISO date range.
        """
        if auth_msg := await _before():
            return auth_msg
        obs = await study_tools.get_patient_observations(
            patient_id=patient_id,
            data_type=data_type,
            start=start,
            end=end,
            base_url=base_url,
        )
        return [o.model_dump() for o in obs]

    return mcp
