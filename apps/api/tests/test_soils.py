from __future__ import annotations

import json

import httpx
import pytest
from shapely.geometry import Polygon, box
from sitesense.soils import (
    QUERY_A,
    QUERY_B,
    SoilsSourceError,
    _prepare_aoi,
    run_soils,
)


class FakeSdaClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = iter(responses)
        self.queries: list[str] = []

    def post(self, _url: str, *, json: dict[str, str]) -> httpx.Response:
        self.queries.append(json["query"])
        request = httpx.Request("POST", _url)
        return httpx.Response(200, request=request, content=jsonlib(next(self.responses)))

    def close(self) -> None:
        pass


def jsonlib(value: dict[str, object]) -> bytes:
    return json.dumps(value).encode()


def _map_units() -> dict[str, object]:
    return {
        "Table": [
            {
                "mukey": "1",
                "musym": "A",
                "muname": "Alpha",
                "acres": 0.2,
                "wkt": "POLYGON ((-97.0 30.0, -96.999 30.0, -96.999 30.001, -97.0 30.001, -97.0 30.0))",
            },
            {
                "mukey": "2",
                "musym": "B",
                "muname": "Beta",
                "acres": 0.8,
                "wkt": "POLYGON ((-96.999 30.0, -96.998 30.0, -96.998 30.001, -96.999 30.001, -96.999 30.0))",
            },
        ]
    }


def _components() -> dict[str, object]:
    return {
        "Table": [
            {
                "mukey": "1", "musym": "A", "muname": "Alpha", "farmlndcl": None,
                "cokey": "11", "compname": "Minor", "comppct_r": 20,
                "slope_l": 1, "slope_r": 2, "slope_h": 3, "drainagecl": "Poorly drained",
                "hydgrp": "D", "taxclname": None, "flodfreqdcd": None, "pondfreqcl": None,
                "runoff": None, "hydricrating": None, "aws0150wta": None,
                "brockdepmin": None, "wtdepannmin": None, "ksat_surface_r": None,
            },
            {
                "mukey": "1", "musym": "A", "muname": "Alpha", "farmlndcl": "Prime",
                "cokey": "12", "compname": "Dominant", "comppct_r": 80,
                "slope_l": 3, "slope_r": 5, "slope_h": 7, "drainagecl": "Well drained",
                "hydgrp": "B", "taxclname": None, "flodfreqdcd": None, "pondfreqcl": None,
                "runoff": None, "hydricrating": None, "aws0150wta": 10,
                "brockdepmin": None, "wtdepannmin": None, "ksat_surface_r": 4,
            },
            {
                "mukey": "2", "musym": "B", "muname": "Beta", "farmlndcl": None,
                "cokey": "21", "compname": "Beta dominant", "comppct_r": 100,
                "slope_l": 1, "slope_r": 2, "slope_h": 4, "drainagecl": "Moderately well drained",
                "hydgrp": "C", "taxclname": None, "flodfreqdcd": "Rare",
                "pondfreqcl": None, "runoff": None, "hydricrating": None, "aws0150wta": None,
                "brockdepmin": None, "wtdepannmin": None, "ksat_surface_r": 2,
            },
        ]
    }


def test_soils_dominant_component_nulls_and_reconciliation() -> None:
    client = FakeSdaClient([_map_units(), _components()])
    result = run_soils(box(-97.0, 30.0, -96.998, 30.001), 10.0, client)
    assert len(result.units) == 2
    assert result.units[0].dominant_component is not None
    assert result.units[0].dominant_component.name == "Dominant"
    assert result.units[0].dominant_component.depth_to_restrictive_layer is None
    assert result.metrics["covered_acres"] > 0
    assert result.metrics["coverage_fraction"] >= 0.99
    assert "JSON+COLUMNNAME" not in client.queries[0]
    assert QUERY_A.splitlines()[0] in client.queries[0]
    assert "WHERE mu.mukey IN ('1', '2')" in client.queries[1]
    assert QUERY_B.splitlines()[0] in client.queries[1]


def test_soils_partial_coverage_warning_uses_submitted_aoi() -> None:
    payload = _map_units()
    rows = payload["Table"]
    assert isinstance(rows, list)
    rows.pop()
    components = _components()
    component_rows = components["Table"]
    assert isinstance(component_rows, list)
    components["Table"] = [row for row in component_rows if row["mukey"] == "1"]
    result = run_soils(
        box(-97.0, 30.0, -96.998, 30.001),
        1.0,
        FakeSdaClient([payload, components]),
    )
    assert result.metrics["coverage_fraction"] < 0.99
    assert result.warnings[0]["code"] == "soils_coverage_incomplete"


def test_soils_no_map_units_is_unavailable_warning() -> None:
    result = run_soils(box(-97.0, 30.0, -96.998, 30.001), 1.0, FakeSdaClient([{"Table": []}]))
    assert result.units == []
    assert result.warnings[0]["code"] == "soils_source_unavailable"


def test_soils_upstream_failure_is_typed() -> None:
    class BrokenClient:
        def post(self, _url: str, **_kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("offline")

        def close(self) -> None:
            pass

    with pytest.raises(SoilsSourceError):
        run_soils(box(-97.0, 30.0, -96.998, 30.001), 1.0, BrokenClient())  # type: ignore[arg-type]


def test_soils_rejects_non_numeric_mukey() -> None:
    payload = _map_units()
    payload["Table"][0]["mukey"] = "1'; DROP TABLE mapunit;--"  # type: ignore[index]
    with pytest.raises(SoilsSourceError, match="non-numeric mukey"):
        run_soils(
            box(-97.0, 30.0, -96.998, 30.001),
            1.0,
            FakeSdaClient([payload]),
        )


def test_soils_bounds_large_aoi_and_reports_geometry_method() -> None:
    coordinates = [
        (-97.0 + index * 0.000001, 30.0 + (index % 2) * 0.001)
        for index in range(5000)
    ]
    geometry = Polygon(coordinates + [coordinates[0]])
    bounded, warnings = _prepare_aoi(geometry)
    assert len(bounded.wkt) <= 7000
    assert warnings
    assert warnings[0]["code"] in {"soils_aoi_simplified", "soils_aoi_envelope_fallback"}
