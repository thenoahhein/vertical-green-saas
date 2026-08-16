import os
import subprocess
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from sitesense.main import app
from sitesense.models import Base, Job, JobStage, Organization, Parcel, Project, Property
from sitesense_worker.tasks import noop_analysis
from sqlalchemy import select, text


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer dev-token"}


def test_migration_downgrade_upgrade(database_context) -> None:
    env = os.environ | {
        "DATABASE_URL": database_context.sync_url,
        "PYTHONPATH": "apps/api/src:apps/worker/src",
    }
    for command in (("downgrade", "base"), ("upgrade", "head")):
        subprocess.run(
            ["uv", "run", "alembic", "-c", "apps/api/alembic.ini", *command],
            check=True,
            cwd="/home/ubuntu/repos/vertical-green-saas",
            env=env,
        )


def _geometry_tables() -> list[tuple[str, str]]:
    return [
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if getattr(column.type, "geometry_type", None)
    ]


@pytest.mark.asyncio
async def test_geometry_insert_and_spatial_indexes(db_sessionmaker, database_context) -> None:
    organization_id = uuid4()
    async with db_sessionmaker() as session:
        session.add(Organization(id=organization_id, name="Geometry Organization"))
        project = Project(organization_id=organization_id, name="Geometry project")
        session.add(project)
        await session.flush()
        property_ = Property(
            organization_id=organization_id,
            project_id=project.id,
            address="1 Main Street",
            geometry=from_shape(Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]), srid=4326),
        )
        session.add(property_)
        await session.flush()
        session.add(
            Parcel(
                organization_id=organization_id,
                property_id=property_.id,
                county="Bastrop",
                appraisal_parcel_id="TEST-1",
                geometry=from_shape(
                    MultiPolygon([Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])]),
                    srid=4326,
                ),
            )
        )
        await session.commit()
        tables = _geometry_tables()
        result = await session.execute(
            text("SELECT tablename, indexdef FROM pg_indexes WHERE indexdef ILIKE '%gist%'")
        )
        indexed_tables = {row.tablename for row in result}
        assert all(table in indexed_tables for table, _ in tables)


@pytest.mark.asyncio
async def test_tenant_isolation_and_project_analysis(db_sessionmaker, seeded_ids, seed_auth) -> None:
    organization_id, _ = seeded_ids
    other_org = uuid4()
    async with db_sessionmaker() as session:
        session.add(Organization(id=other_org, name="Other Organization"))
        session.add(Project(id=UUID("11111111-1111-1111-1111-111111111111"), organization_id=other_org, name="Other"))
        await session.commit()
    client = TestClient(app)
    assert client.get("/api/projects/11111111-1111-1111-1111-111111111111", headers=_headers()).status_code == 404
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    project = client.post("/api/projects", json={"name": "Analysis project"}, headers=_headers())
    assert project.status_code == 201
    project_id = project.json()["id"]
    created = client.post(f"/api/projects/{project_id}/analyze", headers=_headers())
    assert created.status_code == 202
    status_response = client.get(f"/api/projects/{project_id}/analysis/status", headers=_headers())
    assert status_response.status_code == 200
    assert status_response.json()["project_id"] == project_id


def test_job_lifecycle(db_sessionmaker) -> None:
    import asyncio

    async def run() -> None:
        organization_id = uuid4()
        async with db_sessionmaker() as session:
            session.add(Organization(id=organization_id, name="Job Organization"))
            project = Project(organization_id=organization_id, name="Job project")
            session.add(project)
            await session.commit()
            job = Job(organization_id=organization_id, project_id=project.id, stage=JobStage.queued, category_status={})
            session.add(job)
            await session.commit()
            job_id = str(job.id)
        noop_analysis(job_id)
        noop_analysis(job_id, "partial", "groundwater unavailable")
        async with db_sessionmaker() as session:
            partial = await session.scalar(select(Job).where(Job.id == job.id))
            assert partial.stage == JobStage.partial
            assert partial.category_status == {
                "terrain": "complete",
                "groundwater": "unavailable",
            }
            assert partial.error_detail == "groundwater unavailable"
        noop_analysis(job_id, "failed", "worker failure")
        async with db_sessionmaker() as session:
            stored = await session.scalar(select(Job).where(Job.id == job.id))
            assert stored.stage == JobStage.failed
            assert stored.error_detail == "worker failure"

    asyncio.run(run())
