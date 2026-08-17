"""Preliminary USFWS NWI wetland screening for a confirmed parcel."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from sitesense.disclaimers import DISCLAIMERS
from sitesense.geo import acreage

NWI_QUERY_URL = (
    "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/"
    "Wetlands/MapServer/0/query"
)
NWI_SOURCE_URL = NWI_QUERY_URL
NWI_BUFFER_METERS = 152.4
NWI_PAGE_SIZE = 1000
_LENGTH_PROJECTOR = Transformer.from_crs("EPSG:4326", "EPSG:3081", always_xy=True)
_INVERSE_LENGTH_PROJECTOR = Transformer.from_crs("EPSG:3081", "EPSG:4326", always_xy=True)


class WetlandsSourceError(RuntimeError):
    """An upstream NWI request or response could not be used."""


@dataclass(frozen=True)
class WetlandResult:
    geometry: BaseGeometry
    nwi_attribute_code: str | None
    wetland_type: str | None
    acres: float
    source_acres: float | None
    intersects_parcel: bool
    distance_to_parcel_m: float


@dataclass
class WetlandsResult:
    units: list[WetlandResult]
    metrics: dict[str, Any]
    warnings: list[dict[str, Any]]
    source_url: str
    retrieved_at: datetime
    stage_timings: dict[str, float]
    available: bool = True


def _query_geometry(geometry: BaseGeometry) -> dict[str, Any]:
    if isinstance(geometry, Polygon):
        rings: list[list[tuple[float, float]]] = [list(geometry.exterior.coords)]
        rings.extend(list(interior.coords) for interior in geometry.interiors)
    elif isinstance(geometry, MultiPolygon):
        rings = []
        for polygon in geometry.geoms:
            rings.append(list(polygon.exterior.coords))
            rings.extend(list(interior.coords) for interior in polygon.interiors)
    else:
        raise WetlandsSourceError("NWI parcel geometry must be a polygon or multipolygon.")
    return {"rings": rings}


def _rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise WetlandsSourceError("NWI response had an unexpected shape.")
    rows = payload["features"]
    if any(not isinstance(row, dict) or not isinstance(row.get("geometry"), dict) for row in rows):
        raise WetlandsSourceError("NWI response contained invalid features.")
    return cast(list[dict[str, Any]], rows)


def _attribute(attributes: dict[str, Any], name: str) -> Any:
    if name in attributes:
        return attributes[name]
    for key, value in attributes.items():
        if key.casefold() == name.casefold():
            return value
    raise WetlandsSourceError(f"NWI response is missing required field {name!r}.")


def _feature_id(feature: dict[str, Any]) -> str:
    value = feature.get("id")
    if value is None:
        attributes = feature.get("attributes", {})
        if isinstance(attributes, dict):
            value = attributes.get("OBJECTID") or attributes.get("Wetlands.OBJECTID")
    return str(value) if value is not None else json.dumps(feature, sort_keys=True)


def _geometry(value: dict[str, Any]) -> BaseGeometry:
    if value.get("type"):
        return shape(value)
    rings = value.get("rings")
    if not isinstance(rings, list) or not rings:
        raise WetlandsSourceError("NWI feature geometry was invalid.")
    return Polygon(rings[0], [ring for ring in rings[1:] if isinstance(ring, list)])


def _query(
    geometry: BaseGeometry,
    client: httpx.Client,
    label: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = client.get(
            NWI_QUERY_URL,
            params={
                "f": "json",
                "where": "1=1",
                "geometry": json.dumps(_query_geometry(geometry), separators=(",", ":")),
                "geometryType": "esriGeometryPolygon",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(offset),
                "resultRecordCount": str(NWI_PAGE_SIZE),
            },
        )
        response.raise_for_status()
        page = _rows(response.json())
        rows.extend(page)
        if len(page) < NWI_PAGE_SIZE:
            return rows
        offset += len(page)


def _request(
    geometry: BaseGeometry,
    client: httpx.Client,
    label: str,
) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return _query(geometry, client, label)
        except (httpx.HTTPError, WetlandsSourceError) as exc:
            last_error = exc
    raise WetlandsSourceError(f"NWI {label} request failed: {last_error}") from last_error


def run_wetlands(
    parcel_geometry: BaseGeometry,
    parcel_acres: float,
    client: httpx.Client | None = None,
) -> WetlandsResult:
    """Query NWI for parcel wetlands and a 500-foot context buffer."""
    retrieved_at = datetime.now(UTC)
    http_client = client or httpx.Client(timeout=30.0)
    close_client = client is None
    try:
        started = time.perf_counter()
        parcel_projected = transform(_LENGTH_PROJECTOR.transform, parcel_geometry)
        buffered_geometry = transform(
            _INVERSE_LENGTH_PROJECTOR.transform,
            parcel_projected.buffer(NWI_BUFFER_METERS),
        )
        parcel_rows = _request(parcel_geometry, http_client, "parcel")
        buffer_rows = _request(buffered_geometry, http_client, "buffer")
        parcel_ids = {_feature_id(row) for row in parcel_rows}
        merged: dict[str, dict[str, Any]] = {_feature_id(row): row for row in buffer_rows}
        merged.update({_feature_id(row): row for row in parcel_rows})
        units: list[WetlandResult] = []
        for feature in merged.values():
            attributes = feature.get("attributes", feature.get("properties", {}))
            if not isinstance(attributes, dict):
                raise WetlandsSourceError("NWI feature attributes were invalid.")
            code = _attribute(attributes, "Wetlands.ATTRIBUTE")
            wetland_type = _attribute(attributes, "Wetlands.WETLAND_TYPE")
            source_acres_value = _attribute(attributes, "Wetlands.ACRES")
            source_acres = None if source_acres_value is None else float(source_acres_value)
            geometry = _geometry(feature["geometry"])
            intersects = _feature_id(feature) in parcel_ids or geometry.intersects(parcel_geometry)
            clipped = geometry.intersection(parcel_geometry) if intersects else geometry
            if clipped.is_empty:
                continue
            distance = 0.0 if intersects else float(
                transform(_LENGTH_PROJECTOR.transform, geometry).distance(
                    transform(_LENGTH_PROJECTOR.transform, parcel_geometry)
                )
            )
            units.append(
                WetlandResult(
                    geometry=clipped,
                    nwi_attribute_code=None if code is None else str(code),
                    wetland_type=None if wetland_type is None else str(wetland_type),
                    acres=acreage(clipped) if intersects else (source_acres or acreage(geometry)),
                    source_acres=source_acres,
                    intersects_parcel=intersects,
                    distance_to_parcel_m=distance,
                )
            )
        intersected_acres = sum(unit.acres for unit in units if unit.intersects_parcel)
        adjacent_acres = sum(unit.acres for unit in units if not unit.intersects_parcel)
        warnings: list[dict[str, Any]] = [
            {
                "code": "nwi_screening_disclaimer",
                "message": (
                    "NWI is a remote-sensing inventory at approximately 1:12,000; "
                    "jurisdictional determination requires field delineation and USACE review."
                ),
                "disclaimer": DISCLAIMERS["nwi"],
            }
        ]
        if not units:
            warnings.append(
                {
                    "code": "nwi_mapped_absence",
                    "message": "NWI returned no mapped wetland features; this is not evidence that no wetlands are present.",
                }
            )
        timings = {"parcel_query": 0.0, "buffer_query": 0.0}
        timings["wetlands_total"] = time.perf_counter() - started
        return WetlandsResult(
            units=units,
            metrics={
                "parcel_acres": parcel_acres,
                "wetland_acres": intersected_acres,
                "adjacent_wetland_acres": adjacent_acres,
                "wetland_count": sum(unit.intersects_parcel for unit in units),
                "adjacent_wetland_count": sum(not unit.intersects_parcel for unit in units),
                "buffer_meters": NWI_BUFFER_METERS,
                "nwi_resolution": "approximately 1:12,000",
                "preliminary_planning_only": True,
            },
            warnings=warnings,
            source_url=NWI_SOURCE_URL,
            retrieved_at=retrieved_at,
            stage_timings=timings,
        )
    finally:
        if close_client:
            http_client.close()
