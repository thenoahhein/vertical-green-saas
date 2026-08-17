from typing import cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import to_shape
from sitesense_worker.tasks import enqueue_analysis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitesense.models import (
    AnalysisCategory,
    AnalysisLayer,
    DerivedMetric,
    Job,
    JobStage,
    Project,
    SiteAnalysis,
)
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
    hydrology_category = await db.scalar(
        select(AnalysisCategory)
        .where(
            AnalysisCategory.analysis_id == analysis_row.id,
            AnalysisCategory.category == "hydrology",
            AnalysisCategory.organization_id == org.organization_id,
        )
        .order_by(AnalysisCategory.created_at.desc())
        .limit(1)
    )
    soils_category = await db.scalar(
        select(AnalysisCategory)
        .where(
            AnalysisCategory.analysis_id == analysis_row.id,
            AnalysisCategory.category == "soils",
            AnalysisCategory.organization_id == org.organization_id,
        )
        .order_by(AnalysisCategory.created_at.desc())
        .limit(1)
    )
    ecology_category = await db.scalar(
        select(AnalysisCategory)
        .where(
            AnalysisCategory.analysis_id == analysis_row.id,
            AnalysisCategory.category == "ecology",
            AnalysisCategory.organization_id == org.organization_id,
        )
        .order_by(AnalysisCategory.created_at.desc())
        .limit(1)
    )
    metric_result = await db.execute(
        select(DerivedMetric)
        .where(
            DerivedMetric.analysis_id == analysis_row.id,
            DerivedMetric.organization_id == org.organization_id,
        )
    )
    terrain_payload: dict[str, object] = {}
    hydrology_payload: dict[str, object] = {}
    soils_payload: dict[str, object] = {}
    ecology_payload: dict[str, object] = {}
    histogram: dict[str, dict[str, object]] = {}
    for metric in metric_result.scalars():
        if metric.value is None:
            continue
        if metric.name.startswith("slope_bucket:"):
            _, bucket, suffix = metric.name.split(":", 2)
            histogram.setdefault(bucket, {"bucket": bucket})[suffix] = metric.value
        else:
            if metric.category == "hydrology":
                target = hydrology_payload
            elif metric.category == "soils":
                target = soils_payload
            elif metric.category == "ecology":
                target = ecology_payload
            else:
                target = terrain_payload
            target[metric.name] = metric.value
    if histogram:
        for bucket_payload in histogram.values():
            bucket_payload["percentage_denominator"] = "valid slope pixels"
        terrain_payload["slope_histogram"] = list(histogram.values())
    for payload, prefix, target_name in (
        (soils_payload, "hydrologic_group_acres:", "dominant_hydrologic_group"),
        (ecology_payload, "vegetation_type_acres:", "dominant_vegetation_type"),
    ):
        grouped = {
            name.removeprefix(prefix): value
            for name, value in payload.items()
            if name.startswith(prefix) and isinstance(value, (int, float))
        }
        if grouped:
            payload[target_name] = max(grouped, key=lambda name: grouped[name])
    coverage = terrain_payload.get("coverage_fraction")
    warnings: list[dict[str, object]] = []
    if category is not None and "terrain_catalog_unavailable_cached_product" in category.confidence_reason:
        warnings.append(
            {
                "code": "terrain_catalog_unavailable_cached_product",
                "message": category.confidence_reason,
                "analysis_status": "complete_with_cached_source",
            }
        )
    if isinstance(coverage, (int, float)) and coverage == 0:
        warnings.append(
            {
                "code": "terrain_source_unavailable",
                "message": "3DEP elevation coverage is unavailable for this parcel.",
                "missing_fraction": 1.0,
            }
        )
    elif isinstance(coverage, (int, float)) and coverage < 0.99:
        missing_fraction = 1 - coverage
        warnings.append(
            {
                "code": "terrain_coverage_incomplete",
                "message": f"3DEP elevation coverage is incomplete; {missing_fraction:.1%} of the parcel is missing.",
                "missing_fraction": missing_fraction,
            }
        )
    hydrology_layers_result = await db.execute(
        select(AnalysisLayer)
        .where(
            AnalysisLayer.analysis_id == analysis_row.id,
            AnalysisLayer.organization_id == org.organization_id,
            AnalysisLayer.category == "hydro_wbd_context",
        )
    )
    for layer in hydrology_layers_result.scalars():
        membership = layer.layer_metadata.get("membership")
        if isinstance(membership, dict):
            hydrology_payload["wbd_membership"] = membership
    if hydrology_payload.get("window_truncation_warning"):
        warnings.append(
            {
                "code": "hydrology_window_truncated",
                "message": "Flow enters the analysis window boundary; contributing acreage is a lower bound.",
                "contributing_acres_is_lower_bound": True,
            }
        )
    if hydrology_category is not None:
        hydrology_payload["status"] = hydrology_category.status.value
        hydrology_payload["confidence"] = (
            hydrology_category.confidence.value if hydrology_category.confidence else "low"
        )
        hydrology_payload["confidence_reason"] = hydrology_category.confidence_reason
        hydrology_payload["water_feature_review_label"] = (
            "potential water-management investigation areas"
        )
    for category_name, payload, category_row in (
        ("soils", soils_payload, soils_category),
        ("ecology", ecology_payload, ecology_category),
    ):
        if category_row is not None:
            payload["status"] = category_row.status.value
            payload["confidence"] = category_row.confidence.value if category_row.confidence else "low"
            payload["confidence_reason"] = category_row.confidence_reason
            payload["preliminary_planning_only"] = True
            if category_row.status.value == "unavailable":
                warnings.append(
                    {
                        "code": f"{category_name}_source_unavailable",
                        "message": category_row.confidence_reason,
                    }
                )
    return AnalysisRead(
        status=category.status.value if category else "unavailable",
        confidence=category.confidence.value if category and category.confidence else "low",
        confidence_reason=category.confidence_reason if category else "Terrain analysis unavailable.",
        terrain=terrain_payload or None,
        hydrology=hydrology_payload or None,
        soils=soils_payload or None,
        ecology=ecology_payload or None,
        warnings=warnings,
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
