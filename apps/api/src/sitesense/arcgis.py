"""Shared ArcGIS geometry and bounded query helpers."""

from __future__ import annotations

import json
from typing import Any

from shapely import set_precision
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


def parse_polygon_geometry(value: dict[str, Any], label: str) -> BaseGeometry:
    """Parse GeoJSON or ArcGIS rings, preserving multipart exteriors and holes."""
    if value.get("type") and "coordinates" in value:
        try:
            return shape(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} returned invalid GeoJSON geometry.") from exc
    rings = value.get("rings")
    if not isinstance(rings, list) or not rings or not all(isinstance(ring, list) for ring in rings):
        raise ValueError(f"{label} returned invalid polygon rings.")
    candidates: list[Polygon] = []
    for ring in rings:
        polygon = Polygon(ring)
        if not polygon.is_empty and polygon.is_valid and polygon.area > 0:
            candidates.append(polygon)
    if not candidates:
        raise ValueError(f"{label} returned no usable polygon rings.")
    exterior_orientation = max(candidates, key=lambda item: item.area).exterior.is_ccw
    exteriors = [polygon for polygon in candidates if polygon.exterior.is_ccw == exterior_orientation]
    holes = [polygon for polygon in candidates if polygon.exterior.is_ccw != exterior_orientation]
    polygons = [
        Polygon(
            exterior.exterior.coords,
            holes=[
                list(hole.exterior.coords)
                for hole in holes
                if exterior.contains(hole.representative_point())
            ],
        )
        for exterior in exteriors
    ]
    return unary_union(polygons)


def polygon_query_geometry(geometry: BaseGeometry) -> dict[str, Any]:
    if isinstance(geometry, Polygon):
        rings: list[list[tuple[float, float]]] = [list(geometry.exterior.coords)]
        rings.extend(list(interior.coords) for interior in geometry.interiors)
    elif isinstance(geometry, MultiPolygon):
        rings = []
        for polygon in geometry.geoms:
            rings.append(list(polygon.exterior.coords))
            rings.extend(list(interior.coords) for interior in polygon.interiors)
    else:
        raise ValueError("ArcGIS query geometry must be a polygon or multipolygon.")
    return {"rings": rings}


def bounded_query_geometry(geometry: BaseGeometry, max_chars: int = 7000) -> tuple[str, list[dict[str, Any]]]:
    """Return compact JSON geometry and warnings when simplification is necessary."""
    def encoded(candidate: BaseGeometry) -> str:
        rounded = set_precision(candidate, grid_size=0.000001)
        return json.dumps(polygon_query_geometry(rounded), separators=(",", ":"))

    warnings: list[dict[str, Any]] = []
    value = encoded(geometry)
    if len(value) <= max_chars:
        return value, warnings
    for tolerance in (0.000001, 0.000005, 0.00001, 0.00005, 0.0001):
        simplified = geometry.simplify(tolerance, preserve_topology=True)
        value = encoded(simplified)
        if len(value) <= max_chars:
            warnings.append({
                "code": "query_geometry_simplified",
                "message": "The upstream ArcGIS query geometry was simplified to meet request-size limits.",
                "simplification_tolerance_degrees": tolerance,
            })
            return value, warnings
    envelope = geometry.envelope
    value = encoded(envelope)
    warnings.append({
        "code": "query_geometry_envelope_fallback",
        "message": "The upstream ArcGIS query geometry used an envelope fallback to meet request-size limits.",
    })
    return value, warnings
