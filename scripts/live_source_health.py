"""Opt-in live health check; never run this from normal CI."""

import asyncio

import httpx
from sitesense.geocoding import GEOCODER_URL
from sitesense.parcel_sources import COUNTY_SOURCES


async def main() -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        checks = [
            (f"{source.county} parcel metadata", f"{source.url}?f=json")
            for source in COUNTY_SOURCES
        ]
        checks.append(("Census geocoder", GEOCODER_URL + "?address=1311%20Chestnut%20St%2C%20Bastrop%2C%20TX%2078602&benchmark=Public_AR_Current&format=json"))
        for name, url in checks:
            try:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                healthy = not payload.get("error") and ("fields" in payload or "result" in payload)
                print(f"{name}: {'healthy' if healthy else 'unavailable'} ({response.status_code})")
            except Exception as exc:
                print(f"{name}: unavailable ({exc})")


if __name__ == "__main__":
    asyncio.run(main())
