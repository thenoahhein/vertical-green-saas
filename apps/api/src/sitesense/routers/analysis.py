from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sitesense_worker.tasks import enqueue_analysis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitesense.models import Job, JobStage, Project
from sitesense.schemas import AnalysisRead, AnalyzeResponse, JobRead
from sitesense.tenant import CurrentOrg, current_org, get_db, scoped_get

router = APIRouter(prefix="/projects", tags=["analysis"])


@router.post("/{project_id}/analyze", response_model=AnalyzeResponse, status_code=status.HTTP_202_ACCEPTED)
async def analyze(project_id: UUID, db: AsyncSession = Depends(get_db), org: CurrentOrg = Depends(current_org)) -> Job:
    await scoped_get(db, Project, project_id, org)
    job = Job(id=uuid4(), organization_id=org.organization_id, project_id=project_id, stage=JobStage.queued, category_status={})
    db.add(job)
    await db.commit()
    await db.refresh(job)
    enqueue_analysis.delay(str(job.id))
    return job


@router.get("/{project_id}/analysis/status", response_model=JobRead)
async def job_status(project_id: UUID, db: AsyncSession = Depends(get_db), org: CurrentOrg = Depends(current_org)) -> Job:
    await scoped_get(db, Project, project_id, org)
    result = await db.execute(
        select(Job)
        .where(Job.project_id == project_id, Job.organization_id == org.organization_id)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    return job


@router.get("/{project_id}/analysis", response_model=AnalysisRead)
async def analysis(project_id: UUID, db: AsyncSession = Depends(get_db), org: CurrentOrg = Depends(current_org)) -> AnalysisRead:
    await scoped_get(db, Project, project_id, org)
    return AnalysisRead(status="unavailable", confidence="low", confidence_reason="Analysis not implemented yet.")
