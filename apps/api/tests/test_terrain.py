from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import box
from sitesense.terrain import (
    NODATA,
    ONE_METER_DATASET,
    THIRD_ARC_SECOND_DATASET,
    TerrainSelection,
    analyze_elevation,
    read_mosaic,
    select_products,
)


@dataclass
class FakeResponse:
    payload: dict[str, object]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeClient:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses

    def get(self, _url: str, **kwargs: object) -> FakeResponse:
        dataset = str(kwargs["params"]["datasets"])  # type: ignore[index]
        return FakeResponse(self.responses[dataset])


def _item(dataset: str, bounds: tuple[float, float, float, float], url: str) -> dict[str, object]:
    return {
        "title": dataset,
        "downloadURL": url,
        "boundingBox": {
            "minX": bounds[0],
            "minY": bounds[1],
            "maxX": bounds[2],
            "maxY": bounds[3],
        },
    }


def test_source_selection_prefers_complete_one_meter_coverage() -> None:
    client = FakeClient(
        {
            ONE_METER_DATASET: {"items": [_item(ONE_METER_DATASET, (0, 0, 10, 10), "one.tif")]},
            THIRD_ARC_SECOND_DATASET: {"items": []},
        }
    )
    selection = select_products((1, 1, 9, 9), client)
    assert selection.used_fallback is False
    assert selection.products[0].source_url == "one.tif"


def test_source_selection_falls_back_when_one_meter_is_incomplete() -> None:
    client = FakeClient(
        {
            ONE_METER_DATASET: {"items": [_item(ONE_METER_DATASET, (0, 0, 5, 10), "one.tif")]},
            THIRD_ARC_SECOND_DATASET: {
                "items": [_item(THIRD_ARC_SECOND_DATASET, (0, 0, 10, 10), "third.tif")]
            },
        }
    )
    selection = select_products((1, 1, 9, 9), client)
    assert selection == TerrainSelection(
        products=(selection.products[0],),
        used_fallback=True,
        warning="1 m 3DEP coverage did not fully cover the buffered parcel; used 1/3 arc-second DEM.",
    )
    assert selection.products[0].source_url == "third.tif"


def test_source_selection_reports_empty_coverage() -> None:
    client = FakeClient({ONE_METER_DATASET: {"items": []}, THIRD_ARC_SECOND_DATASET: {"items": []}})
    selection = select_products((1, 1, 9, 9), client)
    assert selection.products == ()
    assert selection.warning == "No 3DEP product fully covers the buffered parcel."


def test_known_gradient_has_analytic_slope_and_aspect() -> None:
    rows, columns = np.mgrid[:32, :32]
    elevation = (100 + columns * 0.1 + rows * 0.2).astype("float32")
    result = analyze_elevation(
        elevation,
        from_origin(0, 32, 1, 1),
        "EPSG:26914",
        box(4, 4, 28, 28),
        box(0, 0, 32, 32),
        24 * 24 / 4046.8564224,
    )
    expected_percent = np.hypot(0.1, 0.2) * 100
    assert np.nanmean(result.slope_percent[5:-5, 5:-5]) == pytest.approx(expected_percent, rel=0.03)
    assert result.metrics["slope_statistics_surface"] == "3x3 focal-mean-smoothed elevation"
    assert result.coverage_fraction == 1.0


def test_constant_surface_is_flat_and_has_full_coverage() -> None:
    elevation = np.full((24, 24), 150, dtype="float32")
    result = analyze_elevation(
        elevation,
        from_origin(0, 24, 1, 1),
        "EPSG:26914",
        box(2, 2, 22, 22),
        box(0, 0, 24, 24),
        400 / 4046.8564224,
    )
    assert np.nanmax(result.slope_percent) == pytest.approx(0)
    assert result.metrics["relief_m"] == pytest.approx(0)
    assert result.warning is None


def test_nodata_produces_typed_coverage_warning() -> None:
    elevation = np.full((24, 24), 150, dtype="float32")
    elevation[:, :12] = NODATA
    result = analyze_elevation(
        elevation,
        from_origin(0, 24, 1, 1),
        "EPSG:26914",
        box(2, 2, 22, 22),
        box(0, 0, 24, 24),
        400 / 4046.8564224,
    )
    assert result.warning is not None
    assert result.warning["code"] == "terrain_coverage_incomplete"
    assert result.warning["missing_fraction"] > 0


def test_zero_coverage_produces_source_unavailable_warning() -> None:
    elevation = np.full((24, 24), NODATA, dtype="float32")
    result = analyze_elevation(
        elevation,
        from_origin(0, 24, 1, 1),
        "EPSG:26914",
        box(2, 2, 22, 22),
        box(0, 0, 24, 24),
        400 / 4046.8564224,
    )
    assert result.coverage_fraction == 0
    assert result.warning == {
        "code": "terrain_source_unavailable",
        "message": "3DEP elevation coverage is unavailable for this parcel.",
        "missing_fraction": 1.0,
    }


def test_adjacent_fixture_tiles_mosaic_on_one_grid() -> None:
    from sitesense.terrain import TerrainProduct

    root = Path(__file__).parent / "fixtures" / "terrain"
    products = (
        TerrainProduct(
            "left", ONE_METER_DATASET, str(root / "seam_left.tif"), (500000, 0, 500016, 16), "1 m", None, None
        ),
        TerrainProduct(
            "right", ONE_METER_DATASET, str(root / "seam_right.tif"), (500016, 0, 500032, 16), "1 m", None, None
        ),
    )
    target_bounds = transform_bounds("EPSG:26914", "EPSG:4326", 500000, 0, 500032, 16)
    mosaic, _, _ = read_mosaic(products, target_bounds, "EPSG:26914")
    assert mosaic.shape[0] >= 16
    assert mosaic.shape[1] >= 32
    assert np.all(mosaic[:16, :16] == 100)
    assert np.all(mosaic[:16, 16:] == 200)


def test_bucket_acres_reconcile_to_parcel_area() -> None:
    elevation = np.full((40, 40), 150, dtype="float32")
    parcel = box(5, 5, 35, 35)
    expected_acres = parcel.area / 4046.8564224
    result = analyze_elevation(
        elevation,
        from_origin(0, 40, 1, 1),
        "EPSG:26914",
        parcel,
        box(0, 0, 40, 40),
        expected_acres,
    )
    bucket_acres = sum(float(bucket["acres"]) for bucket in result.metrics["slope_histogram"])
    assert bucket_acres == pytest.approx(expected_acres, rel=0.01)
