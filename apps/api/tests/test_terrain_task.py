from uuid import UUID

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from sitesense import terrain
from sitesense.models import Job, JobStage, Project
from sitesense.terrain import TerrainSourceError
from sitesense_worker import tasks
from sqlalchemy import select


def test_cog_round_trip_preserves_valid_raster_values() -> None:
    source = np.arange(100, dtype="float32").reshape(10, 10)
    content = tasks._write_cog(source, from_origin(0, 10, 1, 1), "EPSG:26914", -9999.0)
    with MemoryFile(content).open() as dataset:
        round_trip = dataset.read(1, masked=True)
    assert round_trip.count() == source.size
    assert float(round_trip.min()) == pytest.approx(float(source.min()))
    assert float(round_trip.max()) == pytest.approx(float(source.max()))
    assert float(round_trip.mean()) == pytest.approx(float(source.mean()))


def test_cog_round_trip_excludes_float32_nodata_sentinel() -> None:
    source = np.array(
        [[terrain.NODATA, 10.0], [20.0, 30.0]],
        dtype="float32",
    )
    content = tasks._write_cog(source, from_origin(0, 2, 1, 1), "EPSG:26914", terrain.NODATA)
    with MemoryFile(content).open() as dataset:
        round_trip = dataset.read(1, masked=True)
    assert round_trip.count() == 3
    assert float(round_trip.min()) == pytest.approx(10.0)
    assert float(round_trip.max()) == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_source_failure_marks_job_partial(
    db_sessionmaker,
    seeded_ids: tuple[UUID, UUID],
    seed_auth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id, _ = seeded_ids
    async with db_sessionmaker() as session:
        project = Project(organization_id=organization_id, name="Terrain source failure")
        session.add(project)
        await session.flush()
        job = Job(organization_id=organization_id, project_id=project.id, category_status={})
        session.add(job)
        await session.commit()
        job_id = str(job.id)

    monkeypatch.setattr(
        tasks,
        "_terrain_analysis",
        lambda session, job: (_ for _ in ()).throw(TerrainSourceError("timeout")),
    )
    assert tasks.terrain_analysis(job_id) == job_id

    async with db_sessionmaker() as session:
        result = await session.execute(select(Job).where(Job.id == job.id))
        persisted = result.scalar_one()
        assert persisted.stage == JobStage.partial
        assert persisted.category_status == {"terrain": "unavailable"}
        assert "Terrain source unavailable" in (persisted.error_detail or "")


@pytest.mark.asyncio
async def test_programming_failure_marks_job_failed_and_reraises(
    db_sessionmaker,
    seeded_ids: tuple[UUID, UUID],
    seed_auth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization_id, _ = seeded_ids
    async with db_sessionmaker() as session:
        project = Project(organization_id=organization_id, name="Terrain programming failure")
        session.add(project)
        await session.flush()
        job = Job(organization_id=organization_id, project_id=project.id, category_status={})
        session.add(job)
        await session.commit()
        job_id = str(job.id)

    monkeypatch.setattr(
        tasks,
        "_terrain_analysis",
        lambda session, job: (_ for _ in ()).throw(TypeError("unexpected bug")),
    )
    with pytest.raises(TypeError, match="unexpected bug"):
        tasks.terrain_analysis(job_id)

    async with db_sessionmaker() as session:
        result = await session.execute(select(Job).where(Job.id == job.id))
        persisted = result.scalar_one()
        assert persisted.stage == JobStage.failed
        assert persisted.category_status == {"terrain": "failed"}
