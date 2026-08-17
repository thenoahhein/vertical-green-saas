from __future__ import annotations

import json

import httpx
import pytest
from shapely.geometry import box
from sitesense.flood import (
    FEMA_AVAILABILITY_URL,
    FEMA_ZONES_URL,
    TWDB_BLE_URL,
    annual_chance,
    run_flood,
)
from sitesense.groundwater import run_groundwater
from sitesense.wetlands import NWI_QUERY_URL, WetlandsSourceError, run_wetlands


def _response(url: str, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("GET", url), content=json.dumps(payload).encode())


class WetlandsClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = iter(pages)

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        assert url == NWI_QUERY_URL
        return _response(url, next(self.pages))

    def close(self) -> None:
        pass


def _wetland_feature() -> dict[str, object]:
    return {
        "id": "wetland-1",
        "geometry": {
            "rings": [[
                [-97.0, 30.0], [-96.999, 30.0], [-96.999, 30.001], [-97.0, 30.001], [-97.0, 30.0],
            ]]
        },
        "attributes": {
            "Wetlands.ATTRIBUTE": "P",
            "Wetlands.WETLAND_TYPE": "Freshwater Emergent Wetland",
            "Wetlands.ACRES": 1.0,
        },
    }


def test_wetlands_requires_qualified_fields_and_paginates() -> None:
    pages = [{"features": [_wetland_feature()]}, {"features": []}, {"features": [_wetland_feature()]}, {"features": []}]
    result = run_wetlands(box(-97.0, 30.0, -96.998, 30.001), 10.0, WetlandsClient(pages))
    assert result.metrics["wetland_count"] == 1
    assert result.units[0].nwi_attribute_code == "P"

    bad = _wetland_feature()
    bad["attributes"] = {"ATTRIBUTE": "P"}
    with pytest.raises(WetlandsSourceError):
        run_wetlands(
            box(-97.0, 30.0, -96.998, 30.001),
            10.0,
            WetlandsClient([{"features": [bad]}, {"features": []}]),
        )


class FloodClient:
    def __init__(self, availability: list[dict[str, object]], zones: list[dict[str, object]], ble: list[dict[str, object]]) -> None:
        self.responses = {
            FEMA_AVAILABILITY_URL: iter(availability),
            FEMA_ZONES_URL: iter(zones),
            TWDB_BLE_URL: iter(ble),
        }

    def get(self, url: str, **_kwargs: object) -> httpx.Response:
        if url == TWDB_BLE_URL:
            raise httpx.ConnectError("offline")
        return _response(url, next(self.responses[url]))

    def close(self) -> None:
        pass


def _flood_feature(attributes: dict[str, object]) -> dict[str, object]:
    return {
        "geometry": {"rings": [[
            [-97.0, 30.0], [-96.999, 30.0], [-96.999, 30.001], [-97.0, 30.001], [-97.0, 30.0],
        ]]},
        "attributes": attributes,
    }


def test_flood_sentinel_metrics_and_ble_isolation() -> None:
    attrs = {
        "DFIRM_ID": "TX", "FLD_AR_ID": "1", "STUDY_TYP": "FEMA",
        "FLD_ZONE": "AE", "ZONE_SUBTY": "FLOODWAY",
        "SFHA_TF": "T", "STATIC_BFE": -9999, "DEPTH": -9999, "VELOCITY": -9999,
        "V_DATUM": "NAVD88", "LEN_UNIT": "FEET", "SOURCE_CIT": "FEMA",
    }
    broken_ble = FloodClient(
        [{"features": [_flood_feature({})]}],
        [{"features": [_flood_feature(attrs)]}],
        [{"error": {"message": "offline"}}],
    )
    result = run_flood(box(-97.0, 30.0, -96.998, 30.001), 10.0, broken_ble)
    zone = result.zones[0]
    assert zone.attributes["STATIC_BFE"] is None
    assert result.metrics["sfha_acres"] > 0
    assert result.metrics["floodway_acres"] > 0
    assert any(warning["code"] == "twdb_ble_partial" for warning in result.warnings)
    assert annual_chance("X", "") == "Minimal mapped hazard (outside 0.2% annual chance floodplain)"
    assert annual_chance("UNKNOWN", "") is None


def test_flood_unavailable_is_not_no_risk() -> None:
    client = FloodClient([{"features": []}], [], [])
    result = run_flood(box(-97.0, 30.0, -96.998, 30.001), 10.0, client)
    assert result.available is False
    assert result.warnings[0]["code"] == "fema_firm_unavailable"


class GroundwaterClient:
    def get(self, _url: str, **_kwargs: object) -> httpx.Response:
        return _response(
            "https://twdb.test",
            {
                "features": [
                    {
                        "geometry": {"x": -97.0, "y": 30.0},
                        "attributes": {
                            "StateWellNumber": "A-1", "WellDepth": 100,
                            "AquiferCodeName": "Carrizo", "PrimaryWaterUse": "Domestic",
                            "WaterLevelObservationType": "Exists", "WaterQualityAvailable": "Y",
                        },
                    },
                    {
                        "geometry": {"x": -97.5, "y": 30.5},
                        "attributes": {
                            "StateWellNumber": "A-2", "WellDepth": 200,
                            "AquiferCodeName": "Carrizo", "PrimaryWaterUse": "Domestic",
                        },
                    },
                ]
            },
        )

    def close(self) -> None:
        pass


def test_groundwater_filters_by_boundary_distance_and_preserves_nulls() -> None:
    result = run_groundwater(box(-97.001, 29.999, -96.999, 30.001), GroundwaterClient(), radius_miles=1.0)
    assert len(result.wells) == 1
    assert result.wells[0].state_well_number == "A-1"
    assert result.wells[0].availability_flags["water_level_observation_type"] == "Exists"
    assert "owner" not in result.wells[0].availability_flags
