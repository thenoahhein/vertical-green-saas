from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from shapely.geometry import box
from sitesense.ecology import EcologicalUnitResult, EcologyResult
from sitesense.models import AnalysisSourceRef, EcologicalUnit, Job, Project, SiteAnalysis, SoilUnit
from sitesense.soils import SoilComponent, SoilsResult, SoilUnitResult
from sitesense_worker.tasks import _persist_ecology, _persist_soils
from sqlalchemy import select


@pytest.mark.asyncio
async def test_soils_and_ecology_persistence_has_provenance(
    db_sessionmaker: Any,
    seeded_ids: tuple[UUID, UUID],
    seed_auth: Any,
) -> None:
    organization_id, _ = seeded_ids
    async with db_sessionmaker() as async_session:
        project = Project(organization_id=organization_id, name="Soils ecology persistence")
        async_session.add(project)
        await async_session.flush()
        job = Job(organization_id=organization_id, project_id=project.id, category_status={})
        analysis = SiteAnalysis(organization_id=organization_id, project_id=project.id)
        async_session.add_all([job, analysis])
        await async_session.commit()
        job_id, analysis_id = job.id, analysis.id

    geometry = box(-97.0, 30.0, -96.999, 30.001)
    component = SoilComponent(
        mukey="1", cokey="11", name="Dominant", percent=80, slope_low=1,
        slope_representative=3, slope_high=5, drainage_class="Well drained",
        hydrologic_group="B", farmland_classification=None, available_water_storage=None,
        ksat=4, depth_to_restrictive_layer=None, flooding_frequency=None, ponding_class=None,
    )
    soil_result = SoilsResult(
        units=[SoilUnitResult(
            geometry=geometry, mukey="1", musym="A", map_unit_name="Alpha", acres=1,
            parcel_percent=100, reported_acres=1, dominant_component=component,
            components=(component,),
        )],
        metrics={"parcel_acres": 1.0, "covered_acres": 1.0, "coverage_fraction": 1.0},
        warnings=[],
        source_url="https://example.test/sda",
        retrieved_at=datetime.now(UTC),
        stage_timings={"map_unit_query": 0.1},
    )
    ecology_result = EcologyResult(
        units=[EcologicalUnitResult(
            geometry=geometry, system_vegetation_type="Prairie", source_classification_code="P",
            acres=1, parcel_percent=100, source="TPWD EMS 2020 vector", layer_id=10,
            layer_name="TexasBlacklandPrairies_L3C32",
        )],
        metrics={"parcel_acres": 1.0, "covered_acres": 1.0, "coverage_fraction": 1.0},
        warnings=[],
        source_url="https://example.test/tpwd",
        retrieved_at=datetime.now(UTC),
        answered_layers=(10,),
        stage_timings={"layer_queries_and_clipping": 0.1},
    )

    from sitesense_worker.tasks import engine
    with engine.begin() as connection:
        from sqlalchemy.orm import Session
        with Session(connection) as session:
            job_row = session.get(Job, job_id)
            analysis_row = session.get(SiteAnalysis, analysis_id)
            assert job_row is not None and analysis_row is not None
            _persist_soils(session, job_row, analysis_row, soil_result)
            _persist_ecology(session, job_row, analysis_row, ecology_result)
            session.commit()

    async with db_sessionmaker() as async_session:
        assert await async_session.scalar(select(SoilUnit).where(SoilUnit.analysis_id == analysis_id)) is not None
        assert await async_session.scalar(
            select(EcologicalUnit).where(EcologicalUnit.analysis_id == analysis_id)
        ) is not None
        refs = (
            await async_session.scalars(
                select(AnalysisSourceRef).where(
                    AnalysisSourceRef.organization_id == organization_id,
                    AnalysisSourceRef.derived_table.in_(
                        ("soil_units", "ecological_units", "analysis_layers")
                    ),
                )
            )
        ).all()
        assert len(refs) == 4
