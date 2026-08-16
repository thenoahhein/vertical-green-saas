"""Seed verified 3DEP tile footprints for catalog-outage recovery.

This is an opt-in live operator utility. It opens each remote COG with
rasterio, records its actual WGS84 footprint, and upserts a DataSource row.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from typing import Any

import rasterio
from pyproj import Transformer
from sitesense.models import DataSource
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

BASE_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/"
    "TX_Central_B1_2017/TIFF/USGS_one_meter_{tile}_TX_Central_B1_2017.tif"
)
DEFAULT_TILES = ("x65y334", "x66y334")
DATASET = "Digital Elevation Model (DEM) 1 meter"


def _product(url: str) -> dict[str, Any]:
    with rasterio.open(url) as dataset:
        transformer = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
        corners = [
            transformer.transform(dataset.bounds.left, dataset.bounds.bottom),
            transformer.transform(dataset.bounds.left, dataset.bounds.top),
            transformer.transform(dataset.bounds.right, dataset.bounds.bottom),
            transformer.transform(dataset.bounds.right, dataset.bounds.top),
        ]
        bounds = (
            min(point[0] for point in corners),
            min(point[1] for point in corners),
            max(point[0] for point in corners),
            max(point[1] for point in corners),
        )
        return {
            "bounds": bounds,
            "crs": str(dataset.crs),
            "width": dataset.width,
            "height": dataset.height,
            "resolution_m": abs(dataset.transform.a),
        }


def seed(tiles: tuple[str, ...], database_url: str) -> None:
    engine = create_engine(database_url)
    with Session(engine) as session:
        for tile in tiles:
            url = BASE_URL.format(tile=tile)
            metadata = _product(url)
            row = session.scalar(select(DataSource).where(DataSource.source_url == url))
            if row is None:
                row = DataSource(
                    name=f"USGS 3DEP {tile} TX Central B1 2017",
                    agency="USGS",
                    dataset_name=DATASET,
                    source_url=url,
                    access_method="tnmaccess-cog",
                )
                session.add(row)
            row.retrieved_at = datetime.now(UTC)
            row.spatial_resolution = "1 m"
            row.notes = json.dumps(
                {
                    "catalog": "TNMAccess",
                    "product_bounds": list(metadata["bounds"]),
                    "verified_raster": {
                        "crs": metadata["crs"],
                        "width": metadata["width"],
                        "height": metadata["height"],
                        "resolution_m": metadata["resolution_m"],
                    },
                }
            )
            session.flush()
            print(json.dumps({"tile": tile, "url": url, **metadata}, default=str))
        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tile", dest="tiles", action="append")
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://sitesense:sitesense@localhost:5432/sitesense",
        ),
    )
    args = parser.parse_args()
    seed(tuple(args.tiles or DEFAULT_TILES), args.database_url)


if __name__ == "__main__":
    main()
