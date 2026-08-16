from typing import cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import to_shape
from sitesense_worker.tasks import enqueue_analysis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitesense.models import AnalysisCategory, AnalysisLayer, Job, JobStage, Project, SiteAnalysis
from sitesense.schemas import AnalysisLayerRead, AnalysisRead, AnalyzeResponse, JobRead
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
    analysis_row = await db.scalar(
        select(SiteAnalysis)
        .where(SiteAnalysis.project_id == project_id, SiteAnalysis.organization_id == org.organization_id)
        .order_by(SiteAnalysis.created_at.desc())
        .limit(1)
    )
    if analysis_row is None:
        return AnalysisRead(status="unavailable", confidence="low", confidence_reason="Analysis not started.")
    category = await db.scalar(
        select(AnalysisCategory)
        .where(
            AnalysisCategory.analysis_id == analysis_row.id,
            AnalysisCategory.category == "terrain",
            AnalysisCategory.organization_id == org.organization_id,
        )
        .order_by(AnalysisCategory.created_at.desc())
        .limit(1)
    )
    terrain_layer = await db.scalar(
        select(AnalysisLayer)
        .where(
            AnalysisLayer.analysis_id == analysis_row.id,
            AnalysisLayer.category == "terrain_dem",
            AnalysisLayer.organization_id == org.organization_id,
        )
        .order_by(AnalysisLayer.created_at.desc())
        .limit(1)
    )
    terrain = terrain_layer.layer_metadata.get("metrics") if terrain_layer else None
    warning = terrain_layer.layer_metadata.get("warning") if terrain_layer else None
    return AnalysisRead(
        status=category.status.value if category else "unavailable",
        confidence=category.confidence.value if category and category.confidence else "low",
        confidence_reason=category.confidence_reason if category else "Terrain analysis unavailable.",
        terrain=terrain if isinstance(terrain, dict) else None,
        warnings=[warning] if isinstance(warning, dict) else [],
    )


@router.get("/{project_id}/layers", response_model=list[AnalysisLayerRead])
async def layers(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    org: CurrentOrg = Depends(current_org),
) -> list[AnalysisLayerRead]:
    await scoped_get(db, Project, project_id, org)
    analysis_row = await db.scalar(
        select(SiteAnalysis)
        .where(SiteAnalysis.project_id == project_id, SiteAnalysis.organization_id == org.organization_id)
        .order_by(SiteAnalysis.created_at.desc())
        .limit(1)
    )
    if analysis_row is None:
        return []
    result = await db.execute(
        select(AnalysisLayer)
        .where(
            AnalysisLayer.analysis_id == analysis_row.id,
            AnalysisLayer.organization_id == org.organization_id,
        )
        .order_by(AnalysisLayer.created_at)
    )
    return [
        AnalysisLayerRead(
            id=layer.id,
            category=layer.category,
            object_store_key=layer.object_store_key,
            geometry=to_shape(cast(WKBElement, layer.geometry)).__geo_interface__
            if layer.geometry is not None
            else None,
            metadata=layer.layer_metadata,
        )
        for layer in result.scalars()
    ]
