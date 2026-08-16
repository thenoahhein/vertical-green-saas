from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString, box
from sitesense.hydrology import (
    HydrologySourceError,
    WhiteboxBinaryError,
    _corridor_contributing_acres,
    _vectorize_regions,
    assign_mapped_water_relationships,
    boundary_inflow_mask,
    fetch_3dhp,
    fetch_wbd_membership,
    run_hydrology,
    stream_threshold_cells_for_resolution,
    whitebox_binary_path,
)


def _whitebox_available() -> bool:
    try:
        whitebox_binary_path()
    except WhiteboxBinaryError:
        return False
    return True


@dataclass
class Response:
    payload: dict[str, Any]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class Client:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self.payloads = payloads

    def get(self, url: str, **kwargs: Any) -> Response:
        if "/wbd/" in url:
            layer = url.split("/wbd/MapServer/", 1)[1].split("/", 1)[0]
            return Response(self.payloads[f"wbd-{layer}"])
        layer = url.rsplit("/", 2)[-2]
        return Response(self.payloads[f"3dhp-{layer}"])

    def close(self) -> None:
        return None


def test_3dhp_and_wbd_fixture_queries() -> None:
    payloads = {
        **{f"3dhp-{layer}": {"features": []} for layer in (20, 30, 40, 50, 60, 80)},
        "3dhp-50": {
            "features": [
                {
                    "attributes": {"featuretypelabel": "Channel Line"},
                    "geometry": {"paths": [[[-97.3, 30.1], [-97.31, 30.11]]]},
                }
            ]
        },
        "wbd-5": {"features": [{"attributes": {"huc10": "1209030102", "name": "Piney Creek-Colorado River"}}]},
        "wbd-6": {"features": [{"attributes": {"huc12": "120903010206", "name": "Copperas Creek-Colorado River"}}]},
    }
    client = Client(payloads)
    result = fetch_3dhp((-97.32, 30.1, -97.3, 30.12), client)
    assert result[50][0]["attributes"]["featuretypelabel"] == "Channel Line"
    membership = fetch_wbd_membership(-97.3119, 30.1101, client)
    assert membership["huc12"]["huc12"] == "120903010206"


def test_reference_service_outage_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenClient(Client):
        def get(self, url: str, **kwargs: Any) -> Response:
            raise OSError("offline")
    with pytest.raises(HydrologySourceError, match="failed"):
        fetch_3dhp((-97.32, 30.1, -97.3, 30.12), BrokenClient({}))


def test_boundary_inflow_uses_d8_direction_not_boundary_accumulation() -> None:
    outward = np.zeros((3, 3), dtype="float32")
    outward[0, 1] = 4  # north, out of the top edge
    assert not boundary_inflow_mask(outward)[0, 1]

    inward = np.zeros((3, 3), dtype="float32")
    inward[0, 1] = 64  # south, into the window from the top edge
    assert boundary_inflow_mask(inward)[0, 1]


def test_stream_threshold_preserves_contributing_area_across_resolutions() -> None:
    area_m2 = 8093.7128448
    one_m = stream_threshold_cells_for_resolution(1.0, area_m2)
    five_m = stream_threshold_cells_for_resolution(5.0, area_m2)
    assert one_m == 8094
    assert five_m == 324
    assert one_m * 1.0**2 >= area_m2
    assert five_m * 5.0**2 >= area_m2

def test_missing_whitebox_binary_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sitesense.hydrology.whitebox_binary_path", lambda: (_ for _ in ()).throw(WhiteboxBinaryError("missing")))
    with pytest.raises(WhiteboxBinaryError, match="missing"):
        run_hydrology(
            np.ones((8, 8), dtype="float32"),
            from_origin(0, 8, 1, 1),
            "EPSG:26914",
            box(2, 2, 6, 6),
            1.0,
        )


def test_v_valley_routes_to_one_coherent_corridor() -> None:
    if not _whitebox_available():
        pytest.fail("CI must warm the WhiteboxTools binary before routing tests.")
    columns = np.indices((64, 64))[1]
    elevation = np.abs(columns - 32).astype("float32")
    result = run_hydrology(
        elevation,
        from_origin(0, 64, 1, 1),
        "EPSG:26914",
        box(20, 20, 44, 44),
        1.0,
        stream_threshold_cells=2,
        ridge_valley_min_length_m=1.0,
    )
    assert result.flow_accumulation[32, 0] <= result.flow_accumulation[32, -1]
    assert result.drainage_lines
    assert result.valleys
    assert result.corridors
    assert any(corridor.parcel_length_m > 0 for corridor in result.corridors)
    assert result.metrics["drainage_vectorization_method"] == "whitebox-raster-streams-to-vector"


def test_subbasin_vectorization_preserves_distinct_ids() -> None:
    values = np.array(
        [
            [1, 1, 0, 2, 2],
            [1, 1, 0, 2, 2],
            [0, 0, 0, 0, 0],
            [3, 3, 3, 0, 0],
            [3, 3, 3, 0, 0],
        ],
        dtype="float32",
    )
    regions = _vectorize_regions(values, from_origin(0, 5, 1, 1), lambda raster: raster > 0)
    assert len(regions) == 3
    assert sorted(round(region.area) for region in regions) == [4, 4, 6]


def test_tilted_plane_routes_downhill() -> None:
    if not _whitebox_available():
        pytest.fail("CI must warm the WhiteboxTools binary before routing tests.")
    rows, cols = np.indices((48, 48))
    elevation = (rows * 0.1 + cols * 0.02).astype("float32")
    result = run_hydrology(
        elevation,
        from_origin(0, 48, 1, 1),
        "EPSG:26914",
        box(10, 10, 38, 38),
        1.0,
        stream_threshold_cells=2,
    )
    assert np.nanmax(result.flow_accumulation[0]) > np.nanmax(result.flow_accumulation[-1])


def test_closed_depression_is_detected_and_filled() -> None:
    if not _whitebox_available():
        pytest.fail("CI must warm the WhiteboxTools binary before routing tests.")
    elevation = np.full((48, 48), 10.0, dtype="float32")
    elevation[22:27, 22:27] = 2.0
    result = run_hydrology(
        elevation,
        from_origin(0, 48, 1, 1),
        "EPSG:26914",
        box(10, 10, 38, 38),
        1.0,
        stream_threshold_cells=2,
    )
    assert result.depressions
    assert all(
        np.isfinite(depression.depth_m) and np.isfinite(depression.volume_m3)
        for depression in result.depressions
    )
    assert float(result.conditioned[24, 24]) > float(elevation[24, 24])


def test_two_channel_confluence_accumulates_downstream() -> None:
    if not _whitebox_available():
        pytest.fail("CI must warm the WhiteboxTools binary before routing tests.")
    rows, cols = np.indices((64, 64))
    elevation = (
        np.minimum(np.abs(cols - 20), np.abs(cols - 44)) * 0.5 - rows * 0.1
    ).astype("float32")
    result = run_hydrology(
        elevation,
        from_origin(0, 64, 1, 1),
        "EPSG:26914",
        box(10, 10, 54, 54),
        1.0,
        stream_threshold_cells=2,
    )
    assert result.drainage_lines
    assert float(result.flow_accumulation[-1].max()) > float(result.flow_accumulation[32].max())


def test_accumulation_uses_pointer_codes_as_cells_and_filters_channel_noise() -> None:
    if not _whitebox_available():
        pytest.fail("CI must warm the WhiteboxTools binary before routing tests.")
    rows, cols = np.indices((32, 32))
    elevation = (np.abs(cols - 16) * 2 - rows).astype("float32")
    result = run_hydrology(
        elevation,
        from_origin(0, 32, 1, 1),
        "EPSG:26914",
        box(10, 10, 22, 22),
        1.0,
    )
    valid_cells = int(result.metrics["valid_cell_count"])
    assert float(result.metrics["max_flow_accumulation_cells"]) >= valid_cells * 0.8
    assert len(result.corridors) <= 5
    assert float(result.flow_accumulation[-1].max()) >= valid_cells * 0.8


def test_corridor_metrics_and_mapped_water_relationships() -> None:
    accumulation = np.zeros((8, 8), dtype="float32")
    accumulation[:, 2] = np.arange(8)
    accumulation[:, 5] = np.arange(8) * 2
    transform = from_origin(0, 8, 1, 1)
    first = LineString([(2.5, 7.5), (2.5, 0.5)])
    second = LineString([(5.5, 7.5), (5.5, 0.5)])
    first_acres = _corridor_contributing_acres(first, accumulation, transform, 1.0)
    second_acres = _corridor_contributing_acres(second, accumulation, transform, 1.0)
    assert second_acres > first_acres

    result = type(
        "Result",
        (),
        {
            "corridors": [
                type("Corridor", (), {"geometry": first, "mapped_water_relationship": ""})(),
            ],
            "metrics": {},
        },
    )()
    assign_mapped_water_relationships(result, [LineString([(2.5, 7.5), (2.5, 0.5)])])
    assert result.corridors[0].mapped_water_relationship == "near mapped 3DHP flowline/waterbody"
    assign_mapped_water_relationships(result, None)
    assert result.corridors[0].mapped_water_relationship == "3DHP hydrography unavailable"


def test_whitebox_path_is_explicit() -> None:
    try:
        path = whitebox_binary_path()
    except WhiteboxBinaryError:
        pytest.skip("WhiteboxTools binary is not present in this test environment")
    assert path.name == "whitebox_tools"
