from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jhe_mcp.config import Settings
from jhe_mcp.tools import study as study_tools


def build_server(settings: Settings) -> FastMCP:
    mcp = FastMCP(name="jhe-mcp")
    base_url = settings.jhe_base_url

    @mcp.tool()
    async def get_study_count() -> int:
        """How many studies the authenticated user can see."""
        return await study_tools.get_study_count(base_url=base_url)

    @mcp.tool()
    async def list_studies() -> list[dict]:
        """List studies the authenticated user can see."""
        studies = await study_tools.list_studies(base_url=base_url)
        return [s.model_dump() for s in studies]

    @mcp.tool()
    async def get_study_metadata(study_id: str) -> dict | None:
        """Metadata for one study by ID. Returns null if not found or not authorized."""
        meta = await study_tools.get_study_metadata(study_id=study_id, base_url=base_url)
        return meta.model_dump() if meta else None

    @mcp.tool()
    async def get_patient_demographics(patient_id: str) -> dict | None:
        """Slim demographics for a patient by ID."""
        d = await study_tools.get_patient_demographics(patient_id=patient_id, base_url=base_url)
        return d.model_dump() if d else None

    @mcp.tool()
    async def get_patient_observations(
        patient_id: str,
        data_type: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        """
        Observations for a patient, optionally filtered by OMH data type short
        name (e.g., 'blood-glucose', 'heart-rate') and ISO date range.
        """
        obs = await study_tools.get_patient_observations(
            patient_id=patient_id,
            data_type=data_type,
            start=start,
            end=end,
            base_url=base_url,
        )
        return [o.model_dump() for o in obs]

    return mcp
