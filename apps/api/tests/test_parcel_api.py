from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import MultiPolygon, Polygon
from sitesense.disclaimers import DISCLAIMERS
from sitesense.geocoding import GeocoderNoMatch
from sitesense.main import app
from sitesense.models import DataSource
from sitesense.parcel_sources import NormalizedParcel
from sitesense.routers import parcels
from sqlalchemy import select


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer dev-token"}


def _candidate() -> NormalizedParcel:
    polygon = Polygon([(-97.0, 30.0), (-97.0, 30.001), (-96.999, 30.001), (-96.999, 30.0), (-97.0, 30.0)])
    return NormalizedParcel(
        UUID(int=101),
        "Bastrop",
        "https://services.arcgis.com/aS4XD9PgZha28y8P/arcgis/rest/services/BastropCADWebService/FeatureServer",
        "OBJECT-1",
        "PROP-1",
        "1 Main St Bastrop TX",
        "LOT 1",
        1.5,
        "Owner",
        MultiPolygon([polygon]),
        {"prop_id_text": "PROP-1"},
        contains_point=True,
    )


def test_search_and_confirm_persist_provenance(
    db_sessionmaker, seed_auth, monkeypatch
) -> None:
    async def fake_search(*args, **kwargs):
        return [_candidate()]

    monkeypatch.setattr(parcels, "search_counties", fake_search)
    client = TestClient(app)
    project = client.post("/api/projects", json={"name": "Parcel project"}, headers=_headers())
    assert project.status_code == 201
    project_id = project.json()["id"]
    search = client.get("/api/parcel-search?latitude=30.0&longitude=-97.0", headers=_headers())
    assert search.status_code == 200
    payload = search.json()
    assert payload["candidates"][0]["appraisal_acres"] == 1.5
    assert payload["candidates"][0]["computed_acres"] > 0

    import asyncio

    async def add_source() -> None:
        async with db_sessionmaker() as session:
            session.add(
                DataSource(
                    name="Bastrop CAD",
                    agency="Bastrop CAD",
                    dataset_name="bastrop_cad_parcels",
                    source_url=_candidate().source_url,
                    access_method="ArcGIS FeatureServer",
                )
            )
            await session.commit()

    asyncio.run(add_source())
    confirmed = client.post(
        f"/api/projects/{project_id}/parcel",
        json={"candidate": payload["candidates"][0]},
        headers=_headers(),
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["disclaimer"] == DISCLAIMERS["parcel_boundary"]
    repeated = client.post(
        f"/api/projects/{project_id}/parcel",
        json={"candidate": payload["candidates"][0]},
        headers=_headers(),
    )
    assert repeated.status_code == 201
    assert repeated.json()["parcel_id"] == confirmed.json()["parcel_id"]

    async def assert_ref() -> None:
        async with db_sessionmaker() as session:
            refs = (await session.execute(select(parcels.AnalysisSourceRef))).scalars().all()
            assert len(refs) == 1

    asyncio.run(assert_ref())


def test_coordinate_fallback_does_not_require_geocoder(monkeypatch) -> None:
    async def unavailable(_address: str):
        raise RuntimeError("geocoder down")

    async def fake_search(*args, **kwargs):
        return []

    monkeypatch.setattr(parcels, "geocode", unavailable)
    monkeypatch.setattr(parcels, "search_counties", fake_search)
    client = TestClient(app)
    response = client.get(
        "/api/parcel-search?address=unknown&latitude=30&longitude=-97",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["latitude"] == 30


def test_geocoder_no_match_is_typed_not_validation_error(monkeypatch) -> None:
    async def unavailable(_address: str):
        raise GeocoderNoMatch("no match")

    monkeypatch.setattr(parcels, "geocode", unavailable)
    async def no_candidates(*args, **kwargs):
        return []

    monkeypatch.setattr(parcels, "search_counties", no_candidates)
    response = TestClient(app).get("/api/parcel-search?address=not-a-real-address", headers=_headers())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "address_not_found"


def test_geocoder_no_match_falls_back_to_address_only_candidates(monkeypatch) -> None:
    async def unavailable(_address: str):
        raise GeocoderNoMatch("no match")

    async def address_search(*args, **kwargs):
        assert args[0] is None
        assert kwargs["address"] == "4664 PIN OAK BRANCH RD, La Grange, TX"
        candidate = _candidate()
        candidate.contains_point = False
        candidate.distance_meters = None
        return [candidate]

    monkeypatch.setattr(parcels, "geocode", unavailable)
    monkeypatch.setattr(parcels, "search_counties", address_search)
    response = TestClient(app).get(
        "/api/parcel-search?address=4664%20PIN%20OAK%20BRANCH%20RD%2C%20La%20Grange%2C%20TX",
        headers=_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["geocoder_failed"] is True
    assert payload["matched_address"] is None
    assert payload["latitude"] == pytest.approx(30.0005)
    assert payload["longitude"] == pytest.approx(-96.9995)
    assert payload["candidates"][0]["distance_meters"] is None
    assert payload["candidates"][0]["contains_point"] is False
