from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sitesense.models import Project
from sitesense.schemas import ProjectCreate, ProjectRead
from sitesense.tenant import CurrentOrg, current_org, get_db, scoped_get, scoped_query

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectRead, status_code=201)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_db), org: CurrentOrg = Depends(current_org)) -> Project:
    project = Project(organization_id=org.organization_id, name=payload.name, client_id=payload.client_id)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("", response_model=list[ProjectRead])
async def list_projects(db: AsyncSession = Depends(get_db), org: CurrentOrg = Depends(current_org)) -> list[Project]:
    result = await db.execute(scoped_query(Project, org))
    return list(result.scalars())


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: UUID, db: AsyncSession = Depends(get_db), org: CurrentOrg = Depends(current_org)) -> Project:
    project = await scoped_get(db, Project, project_id, org)
    return cast(Project, project)
