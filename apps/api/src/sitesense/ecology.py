"""Preliminary TPWD Ecological Mapping Systems vector analysis."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from shapely.geometry.base import BaseGeometry

from sitesense.arcgis import parse_polygon_geometry, polygon_query_geometry
from sitesense.geo import acreage

TPWD_MAPSERVER_URL = (
    "https://tpwd.texas.gov/arcgis/rest/services/Vegetation_Mapping/"
    "Texas_Ecological_Mapping_Systems_Data_2020/MapServer"
)
CANDIDATE_LAYERS = {
    10: "TexasBlacklandPrairies_L3C32",
    4: "EastCentralTexasPlains_L3C33",
    5: "EdwardsPlateau_L3C30",
    11: "WestGulfCoastalPlain_L3C34",
}
LAYER_FIELDS = {layer_id: ("CommonName", "Veg_ID") for layer_id in CANDIDATE_LAYERS}
PAGE_SIZE = 1000


class EcologySourceError(RuntimeError):
    """An upstream TPWD request or response could not be used."""


@dataclass(frozen=True)
class EcologicalUnitResult:
    geometry: BaseGeometry
    system_vegetation_type: str | None
    source_classification_code: str | None
    acres: float
    parcel_percent: float
    source: str
    layer_id: int
    layer_name: str


@dataclass
class EcologyResult:
    units: list[EcologicalUnitResult]
    metrics: dict[str, Any]
    warnings: list[dict[str, Any]]
    source_url: str
    retrieved_at: datetime
    answered_layers: tuple[int, ...]
    stage_timings: dict[str, float]


def _feature_rows(payload: Any, layer_id: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        raise EcologySourceError(f"TPWD layer {layer_id} response had an unexpected shape.")
    rows = payload["features"]
    if any(not isinstance(row, dict) or not isinstance(row.get("geometry"), dict) for row in rows):
        raise EcologySourceError(f"TPWD layer {layer_id} response contained invalid features.")
    return cast(list[dict[str, Any]], rows)


def _layer_fields(payload: Any, layer_id: int) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("fields"), list):
        raise EcologySourceError(f"TPWD layer {layer_id} metadata had an unexpected shape.")
    fields = payload["fields"]
    names = {
        field["name"]
        for field in fields
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }
    expected = set(LAYER_FIELDS[layer_id])
    missing = expected - names
    if missing:
        raise EcologySourceError(
            f"TPWD layer {layer_id} is missing expected fields: {sorted(missing)}"
        )
    return names


def _probe_layer(layer_id: int, client: httpx.Client) -> None:
    response = client.get(f"{TPWD_MAPSERVER_URL}/{layer_id}", params={"f": "json"})
    response.raise_for_status()
    _layer_fields(response.json(), layer_id)


def _query_layer(
    layer_id: int,
    parcel_geometry: BaseGeometry,
    client: httpx.Client,
) -> list[dict[str, Any]]:
    try:
        query_geometry = polygon_query_geometry(parcel_geometry)
    except ValueError as exc:
        raise EcologySourceError(
            "TPWD parcel geometry must be a polygon or multipolygon."
        ) from exc
    features: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = client.get(
            f"{TPWD_MAPSERVER_URL}/{layer_id}/query",
            params={
                "f": "geojson",
                "where": "1=1",
                "geometry": json.dumps(query_geometry, separators=(",", ":")),
                "geometryType": "esriGeometryPolygon",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(offset),
                "resultRecordCount": str(PAGE_SIZE),
            },
        )
        response.raise_for_status()
        payload = response.json()
        page = _feature_rows(payload, layer_id)
        features.extend(page)
        if not payload.get("exceededTransferLimit") and len(page) < min(PAGE_SIZE, 1000):
            break
        if not page:
            break
        offset += len(page)
    return features


def _geometry(feature_geometry: dict[str, Any], layer_id: int) -> BaseGeometry:
    try:
        return parse_polygon_geometry(feature_geometry, f"TPWD layer {layer_id}")
    except ValueError as exc:
        raise EcologySourceError(str(exc)) from exc


def run_ecology(
    parcel_geometry: BaseGeometry,
    parcel_acres: float,
    client: httpx.Client | None = None,
) -> EcologyResult:
    """Query candidate TPWD vector layers and clip returned features to the parcel."""
    retrieved_at = datetime.now(UTC)
    timings: dict[str, float] = {}
    http_client = client or httpx.Client(timeout=20.0)
    close_client = client is None
    try:
        started = time.perf_counter()
        units: list[EcologicalUnitResult] = []
        answered: list[int] = []
        failed_layers: list[int] = []
        for layer_id, layer_name in CANDIDATE_LAYERS.items():
            try:
                _probe_layer(layer_id, http_client)
                features = _query_layer(layer_id, parcel_geometry, http_client)
            except (httpx.HTTPError, EcologySourceError):
                failed_layers.append(layer_id)
                continue
            if not features:
                continue
            answered.append(layer_id)
            try:
                layer_units: list[EcologicalUnitResult] = []
                for feature in features:
                    geometry = _geometry(feature["geometry"], layer_id).intersection(parcel_geometry)
                    if geometry.is_empty:
                        continue
                    attributes = feature.get("properties", feature.get("attributes", {}))
                    if not isinstance(attributes, dict):
                        raise EcologySourceError(
                            f"TPWD layer {layer_id} feature attributes were invalid."
                        )
                    vegetation_field, code_field = LAYER_FIELDS[layer_id]
                    if vegetation_field not in attributes or code_field not in attributes:
                        raise EcologySourceError(
                            f"TPWD layer {layer_id} feature omitted expected fields "
                            f"{vegetation_field!r} and {code_field!r}."
                        )
                    vegetation = attributes[vegetation_field]
                    code = attributes[code_field]
                    unit_acres = acreage(geometry)
                    layer_units.append(EcologicalUnitResult(
                        geometry=geometry,
                        system_vegetation_type=None if vegetation is None else str(vegetation),
                        source_classification_code=None if code is None else str(code),
                        acres=unit_acres,
                        parcel_percent=unit_acres / parcel_acres * 100 if parcel_acres else 0,
                        source="TPWD EMS 2020 vector",
                        layer_id=layer_id,
                        layer_name=layer_name,
                    ))
                units.extend(layer_units)
            except EcologySourceError:
                failed_layers.append(layer_id)
                answered.remove(layer_id)
                continue
        timings["layer_queries_and_clipping"] = time.perf_counter() - started
        if not units:
            if failed_layers:
                raise EcologySourceError(
                    f"TPWD layer queries failed for candidate layers: {sorted(failed_layers)}"
                )
            return EcologyResult(
                [],
                {"parcel_acres": parcel_acres, "coverage_fraction": 0.0, "preliminary_planning_only": True},
                [{"code": "ecology_source_unavailable", "message": "TPWD returned no ecological vector features for the parcel."}],
                TPWD_MAPSERVER_URL, retrieved_at, tuple(answered), timings,
            )
        vegetation_acres: dict[str, float] = {}
        for unit in units:
            label = unit.system_vegetation_type or "Unclassified"
            vegetation_acres[label] = vegetation_acres.get(label, 0) + unit.acres
        covered = sum(unit.acres for unit in units)
        return EcologyResult(
            units,
            {
                "parcel_acres": parcel_acres,
                "covered_acres": covered,
                "coverage_fraction": covered / parcel_acres if parcel_acres else 0,
                "vegetation_type_acres": vegetation_acres,
                "dominant_vegetation_type": max(
                    vegetation_acres,
                    key=lambda name: vegetation_acres[name],
                ),
                "preliminary_planning_only": True,
            },
            [
                {
                    "code": "ecology_partial_source",
                    "message": f"TPWD candidate layers failed: {sorted(failed_layers)}",
                    "failed_layer_ids": sorted(failed_layers),
                }
            ]
            if failed_layers
            else [],
            TPWD_MAPSERVER_URL,
            retrieved_at,
            tuple(answered),
            timings,
        )
    finally:
        if close_client:
            http_client.close()
