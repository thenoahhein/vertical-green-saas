import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
from shapely.geometry import MultiPolygon, Point, Polygon
from sitesense.parcel_sources import (
    COUNTY_SOURCES,
    ArcGISParcelAdapter,
    NormalizedParcel,
    SitusQuery,
    normalize_situs,
    parse_situs_query,
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


def test_situs_normalization_handles_abbreviations_and_apostrophes() -> None:
    assert normalize_situs("12 O'Brien Rd.") == "12 O BRIEN ROAD"
    query = parse_situs_query("12 O'Brien Rd., Bastrop, TX")
    assert query is not None
    assert query.house_number == "12"
    assert query.street_tokens == ("O", "BRIEN")


@pytest.mark.asyncio
async def test_arcgis_paginates_and_runs_situs_query() -> None:
    polygon = {"rings": [[[-97, 30], [-97, 30.001], [-96.999, 30.001], [-96.999, 30], [-97, 30]]]}

    class FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self.payload

    features = [
        {"attributes": {"ObjectID_1": "1", "prop_id_text": "1", "situs_num": "12", "situs_street": "O BRIEN RD"}, "geometry": polygon},
        {"attributes": {"ObjectID_1": "2", "prop_id_text": "2", "situs_num": "12", "situs_street": "O BRIEN RD"}, "geometry": polygon},
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def get(self, url: str, params: dict[str, str]) -> FakeResponse:
            self.calls.append(params)
            if params["where"] == "1=1" and params["resultOffset"] == "0":
                return FakeResponse({"features": [features[0]], "exceededTransferLimit": True})
            if params["where"] == "1=1":
                return FakeResponse({"features": [features[1]], "exceededTransferLimit": False})
            return FakeResponse({"features": [features[0]], "exceededTransferLimit": False})

    client = FakeClient()
    adapter = ArcGISParcelAdapter(COUNTY_SOURCES[0], client=client, max_record_count=1)
    result = await adapter.search(Point(-97, 30), address="12 O'Brien Rd")
    assert {parcel.source_feature_id for parcel in result} == {"1", "2"}
    assert client.calls[0]["resultOffset"] == "0"
    assert client.calls[1]["resultOffset"] == "1"
    assert "situs_num = '12'" in client.calls[2]["where"]


def test_attribute_query_escapes_quotes() -> None:
    adapter = ArcGISParcelAdapter(COUNTY_SOURCES[0])
    params = adapter._attribute_params(SitusQuery("12", ("O'BRIEN",)))
    assert "O''BRIEN" in params["where"]


def test_polygon_distance_and_situs_match_rank_before_centroid() -> None:
    point = Point(0, 0)
    large = MultiPolygon([Polygon([(-0.01, -0.01), (-0.01, 0.01), (0.01, 0.01), (0.01, -0.01), (-0.01, -0.01)])])
    small = MultiPolygon([Polygon([(0.0001, -0.0001), (0.0001, 0.0001), (0.0002, 0.0001), (0.0002, -0.0001), (0.0001, -0.0001)])])

    class FakeAdapter:
        source = COUNTY_SOURCES[0]

        async def search(self, point: Point, buffer_meters: float) -> list[NormalizedParcel]:
            return [
                NormalizedParcel(UUID(int=11), "Bastrop", "source", "small", "small", None, None, None, None, small, {}),
                NormalizedParcel(UUID(int=12), "Bastrop", "source", "large", "large", None, None, None, None, large, {}, contains_point=True),
            ]

    result = asyncio.run(search_counties(point, adapters=(FakeAdapter(),)))
    assert result[0].source_feature_id == "large"


def test_situs_match_ranks_above_spatial_only() -> None:
    point = Point(0, 0)
    class FakeAdapter:
        source = COUNTY_SOURCES[0]

        async def search(self, point: Point, buffer_meters: float, address: str) -> list[NormalizedParcel]:
            return [
                NormalizedParcel(UUID(int=21), "Bastrop", "source", "spatial", "spatial", None, None, None, None, point.buffer(.001), {}),
                NormalizedParcel(UUID(int=22), "Bastrop", "source", "situs", "situs", None, None, None, None, point.buffer(.002), {}, situs_match=True),
            ]

    result = asyncio.run(search_counties(point, address="12 Main St", adapters=(FakeAdapter(),)))
    assert result[0].source_feature_id == "situs"


def test_unsupported_county_fans_out_to_all_sources() -> None:
    class FakeAdapter:
        def __init__(self, county: str) -> None:
            self.source = next(source for source in COUNTY_SOURCES if source.county == county)

        async def search(self, point: Point, buffer_meters: float) -> list[NormalizedParcel]:
            return []

    adapters = tuple(FakeAdapter(source.county) for source in COUNTY_SOURCES)
    result = asyncio.run(search_counties(Point(-97, 30), county="Unknown", adapters=adapters))
    assert result == []


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
