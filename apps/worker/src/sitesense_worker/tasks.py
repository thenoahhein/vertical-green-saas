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
from shapely.ops import transform, unary_union
from sitesense.config import get_settings
from sitesense.hydrology import (
    HYDROGRAPHY_URL,
    WBD_URL,
    HydrologySourceError,
    assign_mapped_water_relationships,
    feature_geometries,
    fetch_3dhp,
    fetch_wbd_membership,
    run_hydrology,
)
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
    TerrainSourceError,
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


def _reference_source_row(
    session: Session,
    *,
    name: str,
    agency: str,
    dataset_name: str,
    source_url: str,
    access_method: str,
    notes: str,
) -> DataSource:
    source = session.scalar(select(DataSource).where(DataSource.source_url == source_url))
    if source is None:
        source = DataSource(
            name=name,
            agency=agency,
            dataset_name=dataset_name,
            source_url=source_url,
            access_method=access_method,
            retrieved_at=datetime.now(UTC),
            notes=notes,
        )
        session.add(source)
        session.flush()
    return source


def _source_ref(session: Session, organization_id: Any, source: DataSource, table: str, derived_id: Any) -> None:
    session.add(
        AnalysisSourceRef(
            organization_id=organization_id,
            data_source_id=source.id,
            derived_table=table,
            derived_id=derived_id,
        )
    )


def _persist_hydrology(
    session: Session,
    job: Job,
    analysis: SiteAnalysis,
    parcel: Parcel,
    parcel_projected: Any,
    inverse: Transformer,
    hydrology: Any,
    contributors: tuple[str, ...],
    buffer_geometry: Any,
    analysis_buffer_meters: float,
) -> list[dict[str, object]]:
    """Persist local hydrology products and return typed warnings."""
    object_prefix = f"{job.organization_id}/{job.project_id}/analysis/{analysis.id}"
    category = AnalysisCategory(
        organization_id=job.organization_id,
        analysis_id=analysis.id,
        category="hydrology",
        status=CategoryStatus.complete,
        confidence=Confidence.medium if hydrology.warnings else Confidence.high,
        confidence_reason=(
            "WhiteboxTools D8 routing scoped to the recorded analysis window."
            + (" Boundary inflow makes contributing acreage a lower bound." if hydrology.warnings else "")
        ),
    )
    session.add(category)
    session.flush()
    units = {
        "analysis_window_pixel_area_m2": "square_metres",
        "stream_threshold_cells": "cells",
        "window_boundary_inflow_cells": "cells",
        "window_boundary_inflow_max_cells": "cells",
        "contributing_acres_within_window": "acres",
        "parcel_acres": "acres",
        "local_depression_count": "count",
        "ridge_segment_count": "count",
        "valley_segment_count": "count",
        "drainage_line_count": "count",
        "catchment_count": "count",
    }
    for name, value in hydrology.metrics.items():
        if value is None or not isinstance(value, (bool, int, float)):
            continue
        session.add(
            DerivedMetric(
                organization_id=job.organization_id,
                analysis_id=analysis.id,
                category="hydrology",
                name=name,
                value=float(value),
                unit="boolean" if isinstance(value, bool) else units.get(name, "number"),
            )
        )
    if hydrology.warnings:
        session.add(
            DerivedMetric(
                organization_id=job.organization_id,
                analysis_id=analysis.id,
                category="hydrology",
                name="window_truncation_warning",
                value=1.0,
                unit="boolean",
            )
        )
    raster_outputs = {
        "hydrology_conditioned_dem": hydrology.conditioned,
        "hydrology_flow_direction": hydrology.flow_direction,
        "hydrology_flow_accumulation": hydrology.flow_accumulation,
    }
    source_rows = list(
        session.scalars(select(DataSource).where(DataSource.source_url.in_(contributors)))
    )
    pending_layers: list[AnalysisLayer] = []
    for layer_name, array in raster_outputs.items():
        key = f"{object_prefix}/{layer_name}.tif"
        output = np.where(np.isfinite(array), array, -9999.0)
        _upload(key, _write_cog(output, hydrology.transform, hydrology.crs, -9999.0))
        layer = AnalysisLayer(
            organization_id=job.organization_id,
            analysis_id=analysis.id,
            category=layer_name,
            object_store_key=key,
            layer_metadata={
                "bounds": list(buffer_geometry.bounds),
                "crs": hydrology.crs,
                "resolution_m": abs(hydrology.transform.a),
                "nodata": -9999.0,
                "analysis_window_buffer_m": analysis_buffer_meters,
                "analysis_scope": "within analysis window",
                "source_urls": list(contributors),
                "whitebox_binary_version": hydrology.metrics["whitebox_binary_version"],
            },
        )
        pending_layers.append(layer)

    def add_geometry_layer(category_name: str, geometry: Any, metadata: dict[str, object]) -> None:
        projected = transform(inverse.transform, geometry)
        pending_layers.append(
            AnalysisLayer(
            organization_id=job.organization_id,
            analysis_id=analysis.id,
            category=category_name,
            geometry=from_shape(projected, srid=4326),
            layer_metadata=metadata,
            )
        )

    for line in hydrology.drainage_lines:
        add_geometry_layer(
            "hydrology_drainage_lines",
            line,
            {
                "threshold_cells": hydrology.metrics["stream_threshold_cells"],
                "analysis_scope": "within analysis window",
                "potential_water_management_review_required": True,
                "vectorization_method": hydrology.metrics["drainage_vectorization_method"],
            },
        )
    if hydrology.catchments:
        add_geometry_layer(
            "hydrology_local_catchments",
            unary_union(hydrology.catchments),
            {"analysis_scope": "within analysis window"},
        )
    if hydrology.depressions:
        add_geometry_layer(
            "hydrology_local_depressions",
            unary_union(hydrology.depressions),
            {
                "label": "potential water-management investigation area",
                "requires_contractor_review": True,
            },
        )
    for line in hydrology.ridgelines:
        add_geometry_layer(
            "hydrology_ridgelines",
            line,
            {"requires_contractor_review": True},
        )
    for line in hydrology.valleys:
        add_geometry_layer(
            "hydrology_major_valleys",
            line,
            {"requires_contractor_review": True},
        )
    for corridor in hydrology.corridors:
        add_geometry_layer(
            "hydrology_corridors",
            corridor.geometry,
            {
                "contributing_acres_within_window": corridor.contributing_acres,
                "parcel_length_ft": corridor.parcel_length_m * 3.280839895,
                "parcel_length_m": corridor.parcel_length_m,
                "flow_direction_degrees": corridor.flow_direction_degrees,
                "mapped_water_relationship": corridor.mapped_water_relationship,
                "mapped_water_tolerance_m": hydrology.metrics["mapped_water_tolerance_m"],
                "contributing_acres_is_lower_bound": bool(
                    hydrology.metrics["contributing_acres_is_lower_bound"]
                ),
            },
        )
    session.add_all(pending_layers)
    session.flush()
    for layer in pending_layers:
        for source in source_rows:
            _source_ref(session, job.organization_id, source, "analysis_layers", layer.id)
    return list(hydrology.warnings)


def _persist_reference_layers(
    session: Session,
    job: Job,
    analysis: SiteAnalysis,
    bbox: tuple[float, float, float, float],
    centroid: tuple[float, float],
) -> tuple[list[dict[str, object]], list[Any] | None]:
    warnings: list[dict[str, object]] = []
    mapped_geometries: list[Any] | None = None
    try:
        features = fetch_3dhp(bbox)
        hydro_source = _reference_source_row(
            session,
            name="USGS 3DHP",
            agency="USGS",
            dataset_name="3D Hydrography Program",
            source_url=HYDROGRAPHY_URL,
            access_method="arcgis-feature-service",
            notes="JSON-queryable reference hydrography; Catchment may be unavailable locally.",
        )
        categories = {
            20: "hydro_3dhp_hydrolocations",
            30: "hydro_3dhp_hydrolocations",
            40: "hydro_3dhp_hydrolocations",
            50: "hydro_3dhp_flowlines",
            60: "hydro_3dhp_waterbodies",
            80: "hydro_3dhp_catchments",
        }
        pending_layers: list[AnalysisLayer] = []
        for layer_id, layer_features in features.items():
            if layer_id in (50, 60):
                mapped_geometries = (mapped_geometries or []) + feature_geometries(layer_features)
            for feature in layer_features:
                geometries = feature_geometries([feature])
                if not geometries:
                    continue
                layer = AnalysisLayer(
                    organization_id=job.organization_id,
                    analysis_id=analysis.id,
                    category=categories[layer_id],
                    geometry=from_shape(geometries[0], srid=4326),
                    layer_metadata={
                        "source_layer": layer_id,
                        "attributes": feature.get("attributes", {}),
                        "reference_only": True,
                    },
                )
                pending_layers.append(layer)
        session.add_all(pending_layers)
        session.flush()
        for layer in pending_layers:
            _source_ref(session, job.organization_id, hydro_source, "analysis_layers", layer.id)
        if not features.get(80):
            warnings.append(
                {
                    "code": "hydro_3dhp_catchment_unavailable",
                    "message": "3DHP Catchment reference data was unavailable for this analysis area.",
                }
            )
    except HydrologySourceError as exc:
        warnings.append({"code": "hydro_3dhp_unavailable", "message": str(exc)})
    try:
        membership = fetch_wbd_membership(*centroid)
        wbd_source = _reference_source_row(
            session,
            name="USGS WBD",
            agency="USGS",
            dataset_name="Watershed Boundary Dataset",
            source_url=WBD_URL,
            access_method="arcgis-map-service",
            notes="HUC10/HUC12 point membership; regional context only.",
        )
        layer = AnalysisLayer(
            organization_id=job.organization_id,
            analysis_id=analysis.id,
            category="hydro_wbd_context",
            layer_metadata={"reference_only": True, "membership": membership},
        )
        session.add(layer)
        session.flush()
        _source_ref(session, job.organization_id, wbd_source, "analysis_layers", layer.id)
    except HydrologySourceError as exc:
        warnings.append({"code": "hydro_wbd_unavailable", "message": str(exc)})
    return warnings, mapped_geometries


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
        session.add(
            DerivedMetric(
                organization_id=job.organization_id,
                analysis_id=analysis.id,
                category="terrain",
                name="coverage_fraction",
                value=0.0,
                unit="fraction",
            )
        )
        _set_job(session, job, JobStage.partial, {"terrain": "unavailable"}, selection.warning)
        return
    elevation, grid_transform, grid_crs, contributors = read_mosaic(
        selection.products,
        buffered_bounds,
        "EPSG:26914",
    )
    contributor_sources = [source for source in sources if source.source_url in contributors]
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
    category = AnalysisCategory(
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
    session.add(category)
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
                "source_urls": contributors,
            },
        )
        session.add(layer)
        session.flush()
        for source in contributor_sources:
            session.add(
                AnalysisSourceRef(
                    organization_id=job.organization_id,
                    data_source_id=source.id,
                    derived_table="analysis_layers",
                    derived_id=layer.id,
                )
            )
    for elevation_m, is_index, geometry in result.contours:
        contour_layer = AnalysisLayer(
            organization_id=job.organization_id,
            analysis_id=analysis.id,
            category="terrain_contours",
            geometry=from_shape(transform(inverse.transform, geometry), srid=4326),
            layer_metadata={
                "interval_feet": 2 if abs(result.transform.a) <= 1 else 5,
                "index_interval_feet": 10,
                "elevation_ft": elevation_m * 3.280839895,
                "is_index": is_index,
                "source_urls": contributors,
            },
        )
        session.add(contour_layer)
        session.flush()
        for source in contributor_sources:
            session.add(
                AnalysisSourceRef(
                    organization_id=job.organization_id,
                    data_source_id=source.id,
                    derived_table="analysis_layers",
                    derived_id=contour_layer.id,
                )
            )
    units = {
        "coverage_fraction": "fraction",
        "parcel_acres": "acres",
        "valid_acres": "acres",
        "elevation_min_m": "metres",
        "elevation_max_m": "metres",
        "elevation_mean_m": "metres",
        "elevation_min_ft": "feet",
        "elevation_max_ft": "feet",
        "elevation_mean_ft": "feet",
        "relief_m": "metres",
        "relief_ft": "feet",
        "mean_slope_percent": "percent",
        "mean_slope_degrees": "degrees",
    }
    metrics = dict(result.metrics)
    metrics["coverage_fraction"] = result.coverage_fraction
    for name, value in metrics.items():
        if name == "slope_histogram" or name == "elevation_units" or name == "slope_statistics_surface":
            continue
        if value is not None:
            session.add(
                DerivedMetric(
                    organization_id=job.organization_id,
                    analysis_id=analysis.id,
                    category="terrain",
                    name=name,
                    value=float(value),
                    unit=units.get(name, "number"),
                )
            )
    for bucket in result.metrics.get("slope_histogram", []):
        bucket_name = str(bucket["bucket"])
        for suffix in ("acres", "percentage"):
            session.add(
                DerivedMetric(
                    organization_id=job.organization_id,
                    analysis_id=analysis.id,
                    category="terrain",
                    name=f"slope_bucket:{bucket_name}:{suffix}",
                    value=float(bucket[suffix]),
                    unit="acres" if suffix == "acres" else "percent_of_valid_slope_pixels",
                )
            )
    if result.warning:
        session.add(
            DerivedMetric(
                organization_id=job.organization_id,
                analysis_id=analysis.id,
                category="terrain",
                name="coverage_missing_fraction",
                value=float(result.warning["missing_fraction"]),
                unit="fraction",
                )
            )
    hydrology_warnings: list[dict[str, object]] = []
    hydrology_buffer_meters = DEFAULT_TERRAIN_BUFFER_METERS
    try:
        hydrology_result = run_hydrology(
            elevation,
            grid_transform,
            grid_crs,
            parcel_projected,
            parcel.computed_acres,
        )
        if hydrology_result.warnings:
            hydrology_buffer_meters = 2000.0
            expanded_projected = parcel_projected.buffer(hydrology_buffer_meters)
            expanded_wgs84 = transform(inverse.transform, expanded_projected)
            expanded_bounds: tuple[float, float, float, float] = (
                float(expanded_wgs84.bounds[0]),
                float(expanded_wgs84.bounds[1]),
                float(expanded_wgs84.bounds[2]),
                float(expanded_wgs84.bounds[3]),
            )
            expanded_selection = select_products(expanded_bounds)
            if expanded_selection.products:
                expanded_elevation, expanded_transform, expanded_crs, expanded_contributors = read_mosaic(
                    expanded_selection.products,
                    expanded_bounds,
                    "EPSG:26914",
                )
                hydrology_result = run_hydrology(
                    expanded_elevation,
                    expanded_transform,
                    expanded_crs,
                    parcel_projected,
                    parcel.computed_acres,
                )
                buffered_projected = expanded_projected
                buffered_bounds = expanded_bounds
                contributors = expanded_contributors
                for product in expanded_selection.products:
                    if product.source_url not in [source.source_url for source in sources]:
                        sources.append(_source_row(session, product))
        reference_warnings, mapped_geometries = _persist_reference_layers(
            session,
            job,
            analysis,
            buffered_bounds,
            (float(source_geometry.centroid.x), float(source_geometry.centroid.y)),
        )
        hydrology_warnings.extend(reference_warnings)
        projected_mapped_geometries = (
            None
            if mapped_geometries is None
            else [transform(projector.transform, geometry) for geometry in mapped_geometries]
        )
        assign_mapped_water_relationships(hydrology_result, projected_mapped_geometries)
        hydrology_warnings.extend(
            _persist_hydrology(
                session,
                job,
                analysis,
                parcel,
                parcel_projected,
                inverse,
                hydrology_result,
                contributors,
                buffered_projected,
                hydrology_buffer_meters,
            )
        )
    except HydrologySourceError as exc:
        hydrology_warnings.append({"code": "hydrology_unavailable", "message": str(exc)})
        session.add(
            AnalysisCategory(
                organization_id=job.organization_id,
                analysis_id=analysis.id,
                category="hydrology",
                status=CategoryStatus.unavailable,
                confidence=Confidence.low,
                confidence_reason=str(exc),
            )
        )
    all_warnings = ([result.warning] if result.warning else []) + hydrology_warnings
    _set_job(
        session,
        job,
        JobStage.partial if all_warnings else JobStage.complete,
        {
            "terrain": "complete",
            "hydrology": "partial" if hydrology_warnings else "complete",
        },
        "; ".join(
            str(warning.get("message"))
            for warning in all_warnings
            if warning and warning.get("message")
        )
        or selection.warning,
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
        except TerrainSourceError as exc:
            session.rollback()
            with Session(engine) as failed_session:
                failed_job = failed_session.get(Job, UUID(job_id))
                if failed_job is not None:
                    _set_job(
                        failed_session,
                        failed_job,
                        JobStage.partial,
                        {"terrain": "unavailable"},
                        f"Terrain source unavailable: {exc}",
                    )
                    failed_session.commit()
        except Exception:
            session.rollback()
            with Session(engine) as failed_session:
                failed_job = failed_session.get(Job, UUID(job_id))
                if failed_job is not None:
                    _set_job(
                        failed_session,
                        failed_job,
                        JobStage.failed,
                        {"terrain": "failed"},
                        "Terrain analysis failed.",
                    )
                    failed_session.commit()
            raise
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
