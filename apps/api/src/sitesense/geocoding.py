from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
GEOGRAPHIES_URL = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"


class GeocoderNoMatch(ValueError):
    pass


@dataclass(frozen=True)
class GeocodedAddress:
    latitude: float
    longitude: float
    matched_address: str | None


async def geocode(address: str, client: httpx.AsyncClient | None = None) -> GeocodedAddress:
    own_client = client is None
    active_client = client or httpx.AsyncClient(timeout=8)
    try:
        for attempt in range(3):
            try:
                response = await active_client.get(
                    GEOCODER_URL,
                    params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
                )
                response.raise_for_status()
                matches = response.json().get("result", {}).get("addressMatches", [])
                if not matches:
                    raise GeocoderNoMatch("Address did not produce a geocoder match")
                match = matches[0]
                coordinates = match["coordinates"]
                return GeocodedAddress(
                    latitude=float(coordinates["y"]),
                    longitude=float(coordinates["x"]),
                    matched_address=match.get("matchedAddress"),
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise RuntimeError("Geocoder failed")
    finally:
        if own_client:
            await active_client.aclose()


async def resolve_county(address: str, client: httpx.AsyncClient | None = None) -> str | None:
    own_client = client is None
    active_client = client or httpx.AsyncClient(timeout=8)
    try:
        response = await active_client.get(
            GEOGRAPHIES_URL,
            params={
                "address": address,
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "layers": "Counties",
                "format": "json",
            },
        )
        response.raise_for_status()
        matches = response.json().get("result", {}).get("addressMatches", [])
        counties = matches[0].get("geographies", {}).get("Counties", []) if matches else []
        return counties[0].get("BASENAME") if counties else None
    except Exception:
        return None
    finally:
        if own_client:
            await active_client.aclose()
