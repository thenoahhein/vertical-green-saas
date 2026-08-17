"""Preliminary TWDB well proximity screening for a confirmed parcel."""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from sitesense.config import get_settings
from sitesense.disclaimers import DISCLAIMERS
from sitesense.geo import project_for_length

GROUNDWATER_URL = "https://services.twdb.texas.gov/arcgis/rest/services/Public/TWDB_Groundwater_database/FeatureServer/0"
GROUNDWATER_QUERY_URL = f"{GROUNDWATER_URL}/query"
GROUNDWATER_PAGE_SIZE = 1000
MILES_TO_METERS = 1609.344


class GroundwaterSourceError(RuntimeError):
    """An upstream TWDB well request or response could not be used."""


@dataclass(frozen=True)
class WellResult:
    geometry: Point
    state_well_number: str | None
    depth: float | None
    aquifer: str | None
    use_type: str | None
    distance_to_parcel_m: float
    availability_flags: dict[str, Any]


@dataclass
class GroundwaterResult:
    wells: list[WellResult]
    metrics: dict[str, Any]
    warnings: list[dict[str, Any]]
    source_url: str
    retrieved_at: datetime
    stage_timings: dict[str, float]
    available: bool = True


def _geometry(value: dict[str, Any]) -> Point:
    if value.get("type"):
        geometry = shape(value)
    else:
        try:
            geometry = Point(float(value["x"]), float(value["y"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise GroundwaterSourceError("TWDB well geometry was invalid.") from exc
    if not isinstance(geometry, Point):
        raise GroundwaterSourceError("TWDB well geometry was not a point.")
    return geometry


def _query(
    parcel_geometry: BaseGeometry,
    radius_miles: float,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    projected = project_for_length(parcel_geometry)
    envelope = projected.buffer(radius_miles * MILES_TO_METERS).envelope
    to_wgs84 = Transformer.from_crs("EPSG:3081", "EPSG:4326", always_xy=True).transform
    envelope_wgs84 = transform(to_wgs84, envelope)
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = client.get(
            GROUNDWATER_QUERY_URL,
            params={
                "f": "json",
                "where": "1=1",
                "geometry": json.dumps({
                    "xmin": envelope_wgs84.bounds[0],
                    "ymin": envelope_wgs84.bounds[1],
                    "xmax": envelope_wgs84.bounds[2],
                    "ymax": envelope_wgs84.bounds[3],
                    "spatialReference": {"wkid": 4326},
                }),
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(offset),
                "resultRecordCount": str(GROUNDWATER_PAGE_SIZE),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
            raise GroundwaterSourceError("TWDB groundwater response had an unexpected shape.")
        page = payload["features"]
        if any(not isinstance(row, dict) or not isinstance(row.get("geometry"), dict) for row in page):
            raise GroundwaterSourceError("TWDB groundwater response contained invalid features.")
        rows.extend(cast(list[dict[str, Any]], page))
        if len(page) < GROUNDWATER_PAGE_SIZE:
            return rows
        offset += len(page)


def run_groundwater(
    parcel_geometry: BaseGeometry,
    client: httpx.Client | None = None,
    radius_miles: float | None = None,
) -> GroundwaterResult:
    radius = float(radius_miles if radius_miles is not None else get_settings().groundwater_radius_miles)
    if radius <= 0:
        raise GroundwaterSourceError("TWDB groundwater search radius must be positive.")
    retrieved_at = datetime.now(UTC)
    http_client = client or httpx.Client(timeout=30.0)
    close_client = client is None
    try:
        started = time.perf_counter()
        last_error: Exception | None = None
        rows: list[dict[str, Any]] = []
        for _ in range(2):
            try:
                rows = _query(parcel_geometry, radius, http_client)
                break
            except (httpx.HTTPError, GroundwaterSourceError) as exc:
                last_error = exc
        else:
            raise GroundwaterSourceError(f"TWDB groundwater request failed: {last_error}") from last_error
        parcel_projected = project_for_length(parcel_geometry)
        wells: list[WellResult] = []
        required = ("StateWellNumber", "WellDepth", "AquiferCodeName", "PrimaryWaterUse")
        for feature in rows:
            attrs = feature.get("attributes", feature.get("properties", {}))
            if not isinstance(attrs, dict):
                raise GroundwaterSourceError("TWDB well attributes were invalid.")
            missing = [field for field in required if field not in attrs]
            if missing:
                raise GroundwaterSourceError(f"TWDB groundwater response missing fields: {missing}")
            point = _geometry(feature["geometry"])
            distance = float(project_for_length(point).distance(parcel_projected))
            if distance > radius * MILES_TO_METERS:
                continue
            depth_value = attrs.get("WellDepth")
            depth = None if depth_value is None else float(depth_value)
            wells.append(WellResult(
                geometry=point,
                state_well_number=None if attrs["StateWellNumber"] is None else str(attrs["StateWellNumber"]),
                depth=depth,
                aquifer=None if attrs["AquiferCodeName"] is None else str(attrs["AquiferCodeName"]),
                use_type=None if attrs["PrimaryWaterUse"] is None else str(attrs["PrimaryWaterUse"]),
                distance_to_parcel_m=distance,
                availability_flags={
                    "water_level_observation_type": attrs.get("WaterLevelObservationType"),
                    "water_quality_available": attrs.get("WaterQualityAvailable"),
                },
            ))
        depths = [well.depth for well in wells if well.depth is not None]
        aquifers = {well.aquifer for well in wells if well.aquifer}
        warnings = [{
            "code": "groundwater_voluntary_incomplete",
            "message": "The TWDB groundwater database is voluntary and incomplete; well counts are not evidence of no groundwater.",
            "disclaimer": DISCLAIMERS["wells"],
        }]
        if not wells:
            warnings.append({
                "code": "groundwater_mapped_absence",
                "message": "No TWDB wells were found within the search radius; this is not evidence that groundwater is absent.",
            })
        return GroundwaterResult(
            wells=wells,
            metrics={
                "well_count": len(wells),
                "well_depth_min": min(depths) if depths else None,
                "well_depth_median": statistics.median(depths) if depths else None,
                "well_depth_max": max(depths) if depths else None,
                "distinct_aquifer_count": len(aquifers),
                "nearest_well_distance_m": min((well.distance_to_parcel_m for well in wells), default=None),
                "search_radius": radius,
                "search_radius_unit": "miles",
                "preliminary_planning_only": True,
            },
            warnings=warnings,
            source_url=GROUNDWATER_URL,
            retrieved_at=retrieved_at,
            stage_timings={"well_query_and_filter": time.perf_counter() - started, "groundwater_total": time.perf_counter() - started},
        )
    finally:
        if close_client:
            http_client.close()
