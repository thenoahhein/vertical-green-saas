from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


@dataclass(frozen=True)
class GeocodedAddress:
    latitude: float
    longitude: float
    matched_address: str | None
    county: str | None = None


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
                    raise ValueError("Address did not produce a geocoder match")
                match = matches[0]
                coordinates = match["coordinates"]
                components = match.get("addressComponents", {})
                county = components.get("county") or components.get("county_name")
                city = str(components.get("city") or "").casefold()
                if county is None and city in {"bastrop", "lee", "fayette", "caldwell"}:
                    county = city.title()
                return GeocodedAddress(
                    latitude=float(coordinates["y"]),
                    longitude=float(coordinates["x"]),
                    matched_address=match.get("matchedAddress"),
                    county=county,
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise RuntimeError("Geocoder failed")
    finally:
        if own_client:
            await active_client.aclose()
