from __future__ import annotations

import asyncio
import json
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.geo import mapping
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitesense.disclaimers import DISCLAIMERS
from sitesense.geo import acreage
from sitesense.geocoding import GeocoderNoMatch, geocode, resolve_county
from sitesense.models import AnalysisSourceRef, DataSource, Parcel, Project, Property
from sitesense.parcel_sources import NormalizedParcel, search_counties
from sitesense.schemas import (
    ConfirmedParcelRead,
    ParcelCandidate,
    ParcelConfirmRequest,
    ParcelSearchResponse,
)
from sitesense.tenant import CurrentOrg, current_org, get_db, scoped_get

router = APIRouter(tags=["parcels"])
project_router = APIRouter(prefix="/projects/{project_id}", tags=["parcels"])


def _candidate(parcel: NormalizedParcel) -> ParcelCandidate:
    return ParcelCandidate(
        candidate_id=parcel.candidate_id,
        county=parcel.county,
        source_url=parcel.source_url,
        source_feature_id=parcel.source_feature_id,
        parcel_id=parcel.parcel_id,
        situs_address=parcel.situs_address,
        legal_description=parcel.legal_description,
        appraisal_acres=parcel.appraisal_acres,
        computed_acres=parcel.computed_acres,
        owner=parcel.owner,
        geometry=cast(dict[str, object], mapping(parcel.geometry)),
        raw_attributes=parcel.raw_attributes,
        distance_meters=parcel.distance_meters,
        contains_point=parcel.contains_point,
    )


def _geometry(value: dict[str, object]) -> BaseGeometry:
    geometry = shape(value)
    if geometry.geom_type == "Polygon":
        return geometry
    if geometry.geom_type == "MultiPolygon":
        return geometry
    raise HTTPException(status_code=422, detail="Confirmed parcel geometry must be polygonal")


def _property_geometry(geometry: BaseGeometry) -> Polygon:
    if isinstance(geometry, Polygon):
        return geometry
    polygons = list(getattr(geometry, "geoms", ()))
    if not polygons:
        raise HTTPException(status_code=422, detail="Confirmed parcel geometry is empty")
    return max(polygons, key=lambda polygon: polygon.area)


@router.get("/parcel-search", response_model=ParcelSearchResponse)
async def parcel_search(
    address: str | None = Query(default=None),
    latitude: float | None = Query(default=None, ge=-90, le=90),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    buffer_meters: float = Query(default=1000, ge=0, le=5000),
) -> ParcelSearchResponse:
    matched_address: str | None = None
    county: str | None = None
    geocoder_failed = False
    if latitude is None or longitude is None:
        if not address:
            raise HTTPException(status_code=422, detail="Provide address or latitude and longitude")
        try:
            result, county = await asyncio.gather(geocode(address), resolve_county(address))
            latitude, longitude = result.latitude, result.longitude
            matched_address = result.matched_address
        except GeocoderNoMatch as exc:
            fallback_health: dict[str, str] = {}
            candidates = await search_counties(
                None,
                buffer_meters=0,
                address=address,
                health=fallback_health,
            )
            if not candidates:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "address_not_found",
                        "message": "Address was not found; place a point on the map or provide latitude and longitude.",
                    },
                ) from exc
            centroid = candidates[0].geometry.centroid
            latitude, longitude = centroid.y, centroid.x
            geocoder_failed = True
            return ParcelSearchResponse(
                candidates=[_candidate(parcel) for parcel in candidates],
                latitude=latitude,
                longitude=longitude,
                matched_address=None,
                geocoder_failed=geocoder_failed,
                source_health=[
                    {"county": source_county, "status": value.split(":", 1)[0], **({"reason": value.split(":", 1)[1]} if ":" in value else {})}
                    for source_county, value in fallback_health.items()
                ],
                disclaimer=DISCLAIMERS["parcel_boundary"],
            )
        except Exception as exc:
            geocoder_failed = True
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "geocoder_unavailable",
                    "message": "Geocoding is unavailable; place a point on the map or provide latitude and longitude.",
                },
            ) from exc
    assert latitude is not None and longitude is not None
    if county and county.casefold().endswith(" county"):
        county = county[:-7].strip()
    health: dict[str, str] = {}
    candidates = await search_counties(
        Point(longitude, latitude),
        county,
        buffer_meters,
        address=address,
        health=health,
    )
    return ParcelSearchResponse(
        candidates=[_candidate(parcel) for parcel in candidates],
        latitude=latitude,
        longitude=longitude,
        matched_address=matched_address,
        geocoder_failed=geocoder_failed,
        source_health=[
            {"county": source_county, "status": value.split(":", 1)[0], **({"reason": value.split(":", 1)[1]} if ":" in value else {})}
            for source_county, value in health.items()
        ],
        disclaimer=DISCLAIMERS["parcel_boundary"],
    )


@project_router.post("/parcel", response_model=ConfirmedParcelRead, status_code=status.HTTP_201_CREATED)
async def confirm_parcel(
    project_id: UUID,
    payload: ParcelConfirmRequest,
    db: AsyncSession = Depends(get_db),
    org: CurrentOrg = Depends(current_org),
) -> ConfirmedParcelRead:
    project = await scoped_get(db, Project, project_id, org)
    candidate = payload.candidate
    source_result = await db.execute(select(DataSource).where(DataSource.source_url == candidate.source_url))
    source = source_result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=422, detail={"code": "unknown_data_source", "message": "Candidate source is not registered"})

    existing_result = await db.execute(
        select(Parcel)
        .join(Property, Parcel.property_id == Property.id)
        .where(
            Parcel.organization_id == org.organization_id,
            Property.project_id == project_id,
            Parcel.appraisal_parcel_id == candidate.parcel_id,
            Parcel.source_id == source.id,
        )
    )
    existing = existing_result.scalar_one_or_none()
    other_parcel_result = await db.execute(
        select(Parcel.id)
        .join(Property, Parcel.property_id == Property.id)
        .where(
            Parcel.organization_id == org.organization_id,
            Property.project_id == project_id,
            or_(
                Parcel.appraisal_parcel_id != candidate.parcel_id,
                Parcel.source_id != source.id,
            ),
        )
        .limit(1)
    )
    if other_parcel_result.scalars().first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "project_already_has_property",
                "message": "This project already has a confirmed property; create a new project to confirm another parcel.",
            },
        )
    geometry = _geometry(candidate.geometry)
    computed_acres = acreage(geometry)
    if existing is None:
        property_ = Property(
            organization_id=org.organization_id,
            project_id=project.id,
            address=candidate.situs_address or "",
            geometry=from_shape(_property_geometry(geometry), srid=4326),
        )
        db.add(property_)
        await db.flush()
        parcel = Parcel(
            organization_id=org.organization_id,
            property_id=property_.id,
            source_id=source.id,
            county=candidate.county,
            appraisal_parcel_id=candidate.parcel_id,
            situs_address=candidate.situs_address,
            legal_description=candidate.legal_description,
            appraisal_record_acres=candidate.appraisal_acres,
            computed_acres=computed_acres,
            raw_source_attributes=candidate.raw_attributes,
            geometry=from_shape(geometry, srid=4326),
        )
        db.add(parcel)
        await db.flush()
        db.add(
            AnalysisSourceRef(
                organization_id=org.organization_id,
                data_source_id=source.id,
                derived_table="parcels",
                derived_id=parcel.id,
            )
        )
        await db.commit()
        await db.refresh(parcel)
    else:
        parcel = existing
    return ConfirmedParcelRead(
        parcel_id=parcel.id,
        project_id=project_id,
        county=parcel.county,
        appraisal_parcel_id=parcel.appraisal_parcel_id,
        situs_address=parcel.situs_address,
        legal_description=parcel.legal_description,
        appraisal_record_acres=parcel.appraisal_record_acres,
        computed_acres=parcel.computed_acres,
        geometry=cast(dict[str, object], mapping(geometry)),
        disclaimer=DISCLAIMERS["parcel_boundary"],
    )


@project_router.get("/parcel", response_model=ConfirmedParcelRead)
async def get_project_parcel(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    org: CurrentOrg = Depends(current_org),
) -> ConfirmedParcelRead:
    await scoped_get(db, Project, project_id, org)
    result = await db.execute(
        select(Parcel, func.ST_AsGeoJSON(Parcel.geometry))
        .join(Property, Parcel.property_id == Property.id)
        .where(Property.project_id == project_id, Parcel.organization_id == org.organization_id)
        .order_by(Parcel.created_at.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Parcel not found")
    parcel, geometry = row
    return ConfirmedParcelRead(
        parcel_id=parcel.id,
        project_id=project_id,
        county=parcel.county,
        appraisal_parcel_id=parcel.appraisal_parcel_id,
        situs_address=parcel.situs_address,
        legal_description=parcel.legal_description,
        appraisal_record_acres=parcel.appraisal_record_acres,
        computed_acres=parcel.computed_acres,
        geometry=json.loads(geometry),
        disclaimer=DISCLAIMERS["parcel_boundary"],
    )
