import json
from pathlib import Path
from uuid import UUID

import pytest
from shapely.geometry import Point
from sitesense.parcel_sources import (
    COUNTY_SOURCES,
    ArcGISParcelAdapter,
    NormalizedParcel,
    search_counties,
)

FIXTURES = Path(__file__).parent / "fixtures" / "parcels"


@pytest.mark.parametrize("index", range(4))
def test_each_county_fixture_maps_explicit_fields(index: int) -> None:
    payload = json.loads((FIXTURES / f"{COUNTY_SOURCES[index].county.lower()}.json").read_text())
    feature = payload["features"][0]
    adapter = ArcGISParcelAdapter(COUNTY_SOURCES[index])
    parcel = adapter._normalize(feature, Point(-97.3, 30.1))

    assert parcel.parcel_id
    assert parcel.geometry.geom_type == "MultiPolygon"
    assert parcel.computed_acres > 0
    assert parcel.raw_attributes


def test_sparse_and_empty_responses_preserve_unavailable_values() -> None:
    sparse = json.loads((FIXTURES / "sparse.json").read_text())["features"][0]
    parcel = ArcGISParcelAdapter(COUNTY_SOURCES[0])._normalize(sparse, Point(-97, 30))
    assert parcel.appraisal_acres is None
    assert parcel.situs_address is None
    assert parcel.legal_description is None
    assert json.loads((FIXTURES / "empty.json").read_text())["features"] == []


def test_malformed_response_is_not_silently_valid() -> None:
    with pytest.raises(json.JSONDecodeError):
        json.loads((FIXTURES / "malformed.json").read_text())


@pytest.mark.asyncio
async def test_search_ranks_containing_parcel_first() -> None:
    point = Point(-97.3, 30.1)

    class FakeAdapter:
        async def search(self, point: Point, buffer_meters: float) -> list[NormalizedParcel]:
            inside = NormalizedParcel(UUID(int=1), "Bastrop", "source", "1", "1", None, None, None, None, point.buffer(.01), {}, 10, True)
            outside = NormalizedParcel(UUID(int=2), "Bastrop", "source", "2", "2", None, None, None, None, point.buffer(.001), {}, 1, False)
            return [outside, inside]

    result = await search_counties(point, adapters=(FakeAdapter(),))
    assert result[0].contains_point is True
