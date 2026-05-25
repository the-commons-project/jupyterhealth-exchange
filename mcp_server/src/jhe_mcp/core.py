from __future__ import annotations

from collections.abc import Awaitable, Callable

from mcp.server.fastmcp import FastMCP

from jhe_mcp.auth.oauth_flow import AuthenticationRequired
from jhe_mcp.config import Settings
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
    mcp = FastMCP(name="jhe-mcp")
    base_url = settings.jhe_base_url

    async def _before() -> str | None:
        if pre_tool_hook is not None:
            try:
                await pre_tool_hook()
            except AuthenticationRequired as exc:
                return AUTH_REQUIRED_MSG.format(url=exc.url)
        return None

    @mcp.tool()
    async def get_data_schema() -> dict | str:
        """Describes how observation data is structured in JupyterHealth Exchange.

        Call this first to understand the data model before querying observations.
        Explains the relationship between FHIR Observation resources and their
        Open mHealth (OMH) valueAttachment payloads, including body structure
        examples for each measurement type (blood pressure, heart rate, etc.).
        """
        if auth_msg := await _before():
            return auth_msg
        return study_tools.get_data_schema_description()

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
