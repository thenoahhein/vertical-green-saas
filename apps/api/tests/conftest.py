import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sitesense.config import get_settings
from sitesense.db import get_db
from sitesense.main import app
from sitesense.models import Organization, OrganizationUser, User
from sitesense_worker.tasks import configure_database
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class DatabaseContext:
    sync_url: str
    async_url: str
    schema: str


def _database_url(raw_url: str, database: str) -> URL:
    url = make_url(raw_url)
    return url.set(database=database)


@pytest.fixture(scope="session")
def database_context() -> Iterator[DatabaseContext]:
    raw_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://sitesense:sitesense@localhost:5432/sitesense",
    )
    database = f"test_{uuid4().hex}"
    raw = make_url(raw_url)
    admin_url = raw.set(database="postgres")
    admin_engine = create_engine(admin_url)
    with admin_engine.connect() as connection:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    admin_engine.dispose()

    sync_url = _database_url(raw_url, database).render_as_string(hide_password=False)
    async_url = sync_url.replace("postgresql+psycopg://", "postgresql+psycopg://")
    env = os.environ | {
        "DATABASE_URL": sync_url,
        "PYTHONPATH": "apps/api/src:apps/worker/src",
    }
    for command in ("upgrade head",):
        subprocess.run(
            ["uv", "run", "alembic", "-c", "apps/api/alembic.ini", *command.split()],
            check=True,
            cwd=REPO_ROOT,
            env=env,
        )
    context = DatabaseContext(sync_url=sync_url, async_url=async_url, schema=database)
    yield context
    admin_engine = create_engine(admin_url)
    with admin_engine.connect() as connection:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database AND pid <> pg_backend_pid()"
            ),
            {"database": database},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
    admin_engine.dispose()


@pytest.fixture
def db_sessionmaker(database_context: DatabaseContext) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(database_context.async_url)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def override_database(
    database_context: DatabaseContext,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> Iterator[None]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    configure_database(database_context.sync_url)
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def seeded_ids() -> tuple[UUID, UUID]:
    settings = get_settings()
    return UUID(settings.dev_organization_id), UUID(settings.dev_user_id)


@pytest_asyncio.fixture
async def seed_auth(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    seeded_ids: tuple[UUID, UUID],
) -> None:
    organization_id, user_id = seeded_ids
    async with db_sessionmaker() as session:
        if await session.scalar(select(Organization.id).where(Organization.id == organization_id)) is None:
            session.add(Organization(id=organization_id, name="Test Organization"))
        if await session.scalar(select(User.id).where(User.id == user_id)) is None:
            session.add(User(id=user_id, email="test@example.com", name="Test User"))
        await session.flush()
        if await session.scalar(
            select(OrganizationUser.organization_id).where(
                OrganizationUser.organization_id == organization_id,
                OrganizationUser.user_id == user_id,
            )
        ) is None:
            session.add(OrganizationUser(organization_id=organization_id, user_id=user_id, role="owner"))
        await session.commit()
