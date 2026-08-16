from __future__ import annotations

import io
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import boto3
import numpy as np
import rasterio
from celery import Celery
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from pyproj import Transformer
from rasterio.shutil import copy as copy_raster
from shapely.ops import transform
from sitesense.config import get_settings
from sitesense.jobs import transition_job
from sitesense.models import (
    AnalysisCategory,
    AnalysisLayer,
    AnalysisSourceRef,
    CategoryStatus,
    Confidence,
    DataSource,
    DerivedMetric,
    Job,
    JobStage,
    Parcel,
    Property,
    SiteAnalysis,
)
from sitesense.terrain import (
    DEFAULT_TERRAIN_BUFFER_METERS,
    analyze_elevation,
    read_mosaic,
    select_products,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

settings = get_settings()
celery_app = Celery("sitesense", broker=settings.redis_url, backend=settings.redis_url)
engine = create_engine(settings.database_url)


def configure_database(database_url: str) -> None:
    global engine
    engine.dispose()
    engine = create_engine(database_url)


def _object_store() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.object_store_endpoint,
        aws_access_key_id=settings.object_store_access_key,
        aws_secret_access_key=settings.object_store_secret_key,
        region_name="us-east-1",
    )


def _write_cog(array: np.ndarray, transform_: Any, crs: str, nodata: float) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        source_path = Path(directory) / "source.tif"
        output_path = Path(directory) / "output.tif"
        with rasterio.open(
            source_path,
            "w",
            driver="GTiff",
            height=array.shape[0],
            width=array.shape[1],
            count=1,
            dtype="float32",
            crs=crs,
            transform=transform_,
            nodata=nodata,
            tiled=True,
            blockxsize=256,
            blockysize=256,
            compress="DEFLATE",
        ) as dataset:
            dataset.write(array.astype("float32"), 1)
        copy_raster(source_path, output_path, driver="COG", compress="DEFLATE", overview_resampling="average")
        return output_path.read_bytes()


def _upload(key: str, content: bytes) -> None:
    _object_store().put_object(
        Bucket=settings.object_store_bucket,
        Key=key,
        Body=io.BytesIO(content),
        ContentType="image/tiff",
    )


def _source_row(session: Session, product: Any) -> DataSource:
    source = session.scalar(select(DataSource).where(DataSource.source_url == product.source_url))
    if source is None:
        source = DataSource(
            name=f"USGS 3DEP {product.title}",
            agency="USGS",
            dataset_name=product.dataset_name,
            source_url=product.source_url,
            access_method="tnmaccess-cog",
            version=product.published_at.isoformat() if product.published_at else None,
            published_at=product.published_at,
            retrieved_at=datetime.now(UTC),
            spatial_resolution=product.spatial_resolution,
            notes="TNMAccess-reported byte size is metadata only and is not used as an integrity check.",
        )
        session.add(source)
        session.flush()
    return source


def _set_job(session: Session, job: Job, stage: JobStage, statuses: dict[str, str], detail: str | None = None) -> None:
    job.stage = stage
    job.category_status = statuses
    job.error_detail = detail
    if stage in (JobStage.fetching, JobStage.processing, JobStage.derivatives):
        job.started_at = job.started_at or datetime.now(UTC)
    if stage in (JobStage.complete, JobStage.partial, JobStage.failed):
        job.finished_at = datetime.now(UTC)
    session.flush()


def _terrain_analysis(session: Session, job: Job) -> None:
    parcel = session.scalar(
        select(Parcel)
        .join(Property, Property.id == Parcel.property_id)
        .where(Property.project_id == job.project_id, Parcel.organization_id == job.organization_id)
        .order_by(Parcel.created_at.desc())
    )
    if parcel is None:
        raise ValueError("A confirmed parcel is required before terrain analysis.")
    source_geometry = to_shape(cast(WKBElement, parcel.geometry))
    projector = Transformer.from_crs("EPSG:4326", "EPSG:26914", always_xy=True)
    inverse = Transformer.from_crs("EPSG:26914", "EPSG:4326", always_xy=True)
    parcel_projected = transform(projector.transform, source_geometry)
    buffered_projected = parcel_projected.buffer(DEFAULT_TERRAIN_BUFFER_METERS)
    buffered_wgs84 = transform(inverse.transform, buffered_projected)
    buffered_bounds = (
        float(buffered_wgs84.bounds[0]),
        float(buffered_wgs84.bounds[1]),
        float(buffered_wgs84.bounds[2]),
        float(buffered_wgs84.bounds[3]),
    )
    selection = select_products(buffered_bounds)
    sources = [_source_row(session, product) for product in selection.products]
    if not selection.products:
        analysis = SiteAnalysis(
            organization_id=job.organization_id,
            project_id=job.project_id,
            parcel_id=parcel.id,
        )
        session.add(analysis)
        session.flush()
        session.add(
            AnalysisCategory(
                organization_id=job.organization_id,
                analysis_id=analysis.id,
                category="terrain",
                status=CategoryStatus.unavailable,
                confidence=Confidence.low,
                confidence_reason=selection.warning or "No 3DEP product covered the buffered parcel.",
            )
        )
        _set_job(session, job, JobStage.partial, {"terrain": "unavailable"}, selection.warning)
        return
    elevation, grid_transform, grid_crs = read_mosaic(
        selection.products,
        buffered_bounds,
        "EPSG:26914",
    )
    _set_job(session, job, JobStage.processing, {"terrain": "processing"})
    result = analyze_elevation(
        elevation,
        grid_transform,
        grid_crs,
        parcel_projected,
        buffered_projected,
        parcel.computed_acres,
    )
    _set_job(session, job, JobStage.derivatives, {"terrain": "complete"})
    analysis = SiteAnalysis(
        organization_id=job.organization_id,
        project_id=job.project_id,
        parcel_id=parcel.id,
    )
    session.add(analysis)
    session.flush()
    session.add(
        AnalysisCategory(
            organization_id=job.organization_id,
            analysis_id=analysis.id,
            category="terrain",
            status=CategoryStatus.complete,
            confidence=Confidence.high if result.coverage_fraction >= 0.99 else Confidence.medium,
            confidence_reason=(
                "1 m 3DEP coverage and 3x3 focal-mean-smoothed planning-grade slope statistics."
                if not selection.used_fallback
                else "1/3 arc-second fallback coverage and planning-grade slope statistics."
            ),
        )
    )
    session.flush()
    object_prefix = f"{job.organization_id}/{job.project_id}/analysis/{analysis.id}"
    for category_name, array in {
        "terrain_dem": result.elevation,
        "terrain_slope": result.slope_percent,
        "terrain_hillshade": result.hillshade,
    }.items():
        output = np.where(result.buffer_mask & np.isfinite(array), array, -3.4028230607370965e38)
        key = f"{object_prefix}/{category_name}.tif"
        _upload(key, _write_cog(output, result.transform, result.crs, -3.4028230607370965e38))
        layer = AnalysisLayer(
            organization_id=job.organization_id,
            analysis_id=analysis.id,
            category=category_name,
            object_store_key=key,
            layer_metadata={
                "bounds": list(buffered_projected.bounds),
                "crs": result.crs,
                "resolution_m": abs(result.transform.a),
                "nodata": -3.4028230607370965e38,
                "source_urls": [product.source_url for product in selection.products],
                "metrics": result.metrics if category_name == "terrain_dem" else {},
                "warning": result.warning,
            },
        )
        session.add(layer)
        session.flush()
        for source in sources:
            session.add(
                AnalysisSourceRef(
                    organization_id=job.organization_id,
                    data_source_id=source.id,
                    derived_table="analysis_layers",
                    derived_id=layer.id,
                )
            )
    contour_geometries = [
        transform(inverse.transform, geometry)
        for _, _, geometry in result.contours
    ]
    if contour_geometries:
        from shapely.ops import unary_union

        contour_layer = AnalysisLayer(
            organization_id=job.organization_id,
            analysis_id=analysis.id,
            category="terrain_contours",
            geometry=from_shape(unary_union(contour_geometries), srid=4326),
            layer_metadata={
                "interval_feet": 2 if abs(result.transform.a) <= 1 else 5,
                "index_interval_feet": 10,
                "source_urls": [product.source_url for product in selection.products],
            },
        )
        session.add(contour_layer)
        session.flush()
        for source in sources:
            session.add(
                AnalysisSourceRef(
                    organization_id=job.organization_id,
                    data_source_id=source.id,
                    derived_table="analysis_layers",
                    derived_id=contour_layer.id,
                )
            )
    for name, value in (
        ("coverage_fraction", result.coverage_fraction),
        ("elevation_min_m", result.metrics.get("elevation_min_m")),
        ("elevation_max_m", result.metrics.get("elevation_max_m")),
        ("elevation_mean_m", result.metrics.get("elevation_mean_m")),
        ("relief_m", result.metrics.get("relief_m")),
        ("mean_slope_percent", result.metrics.get("mean_slope_percent")),
        ("mean_slope_degrees", result.metrics.get("mean_slope_degrees")),
    ):
        if value is not None:
            session.add(
                DerivedMetric(
                    organization_id=job.organization_id,
                    analysis_id=analysis.id,
                    category="terrain",
                    name=name,
                    value=float(value),
                    unit="fraction" if name == "coverage_fraction" else "metres",
                )
            )
    _set_job(
        session,
        job,
        JobStage.partial if result.warning else JobStage.complete,
        {"terrain": "complete"},
        result.warning["message"] if result.warning else selection.warning,
    )


@celery_app.task(name="sitesense.terrain_analysis")  # type: ignore[untyped-decorator]
def terrain_analysis(job_id: str) -> str:
    with Session(engine) as session:
        job = session.get(Job, UUID(job_id))
        if job is None:
            raise ValueError(f"Analysis job {job_id} not found")
        try:
            _set_job(session, job, JobStage.fetching, {"terrain": "fetching"})
            _terrain_analysis(session, job)
            session.commit()
        except Exception as exc:
            session.rollback()
            with Session(engine) as failed_session:
                failed_job = failed_session.get(Job, UUID(job_id))
                if failed_job is not None:
                    _set_job(
                        failed_session,
                        failed_job,
                        JobStage.partial,
                        {"terrain": "unavailable"},
                        f"Terrain analysis warning: {exc}",
                    )
                    failed_session.commit()
        return job_id


enqueue_analysis = terrain_analysis


def noop_analysis(job_id: str, outcome: str = "complete", error_detail: str | None = None) -> str:
    """Retain the foundation task contract for lifecycle compatibility tests."""
    with engine.begin() as connection:
        if outcome == "failed":
            transition_job(connection, job_id, JobStage.failed, {"foundation": "failed"}, error_detail or "Analysis failed.")
        elif outcome == "partial":
            transition_job(
                connection,
                job_id,
                JobStage.partial,
                {"terrain": "complete", "groundwater": "unavailable"},
                error_detail,
            )
        else:
            transition_job(connection, job_id, JobStage.complete, {"foundation": "complete"}, error_detail)
    return job_id
