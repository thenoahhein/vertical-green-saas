from uuid import UUID

import pytest
from sitesense.models import Job, JobStage, Project
from sitesense.terrain import TerrainSourceError
from sitesense_worker import tasks
from sqlalchemy import select


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
