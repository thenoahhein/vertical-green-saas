from datetime import UTC, datetime

from sitesense.config import get_settings
from sitesense.models import DataSource, Organization, OrganizationUser, User
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

SOURCES = [
    ("TxGIO StratMap statewide parcels", "TxGIO", "StratMap Land Parcels", "https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/stratmap_land_parcels_48_most_recent/MapServer", "bulk-download", "Public query disabled; bulk-download-only."),
    ("Bastrop CAD parcels", "Bastrop CAD", "BastropCADWebService", "https://services.arcgis.com/aS4XD9PgZha28y8P/arcgis/rest/services/BastropCADWebService/FeatureServer", "arcgis-feature-service", "Layer 0 Parcels polygon."),
    ("Lee CAD parcels", "Lee CAD", "LeeCADWebService", "https://services1.arcgis.com/la5KbvGUYLup9Aee/arcgis/rest/services/LeeCADWebService/FeatureServer", "arcgis-feature-service", "Layer 0 Parcels polygon."),
    ("Fayette CAD parcels", "Fayette CAD", "FayetteCADWebService", "https://services7.arcgis.com/INOomfRKQGxc9OW4/arcgis/rest/services/FayetteCADWebService/FeatureServer", "arcgis-feature-service", "Layer 0 Parcels polygon."),
    ("Caldwell CAD parcels", "Caldwell CAD", "CaldwellCADWebService", "https://services.arcgis.com/rVxY74DxxIDrDbc0/arcgis/rest/services/CaldwellCADWebService/FeatureServer", "arcgis-feature-service", "Layer 0 Parcels polygon."),
    ("USGS 3DEP", "USGS", "3DEP staged COGs", "s3://prd-tnm/StagedProducts/Elevation/13/TIFF/", "s3-public", "Public S3 bucket; TNMAccess API is queryable."),
    ("USDA Soil Data Access", "USDA NRCS", "Soil Data Access", "https://SDMDataAccess.sc.egov.usda.gov/Tabular/post.rest", "http-post", "POST REST endpoint; GET is not supported."),
    ("TWDB groundwater", "TWDB", "Groundwater database", "https://services.twdb.texas.gov/arcgis/rest/services/Public/TWDB_Groundwater_database/FeatureServer", "arcgis-feature-service", ""),
    ("FEMA NFHL", "FEMA", "National Flood Hazard Layer", "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer", "arcgis-map-server", ""),
    ("USFWS NWI", "USFWS", "National Wetlands Inventory", "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer", "arcgis-map-server", "Screening information only; not a jurisdictional determination."),
    ("USGS WBD", "USGS", "Watershed Boundary Dataset", "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer", "arcgis-map-service", "Layers 5 and 6 provide HUC10/HUC12 point membership."),
    ("USGS 3DHP", "USGS", "3D Hydrography Program", "https://3dhp.nationalmap.gov/arcgis/rest/services/usgs_3dhp_all/FeatureServer", "arcgis-feature-service", "JSON-queryable layers 20, 30, 40, 50, 60, and 80; Catchment may be unavailable locally."),
    ("TPWD EMS", "TPWD", "Ecological Mapping Systems", "https://tpwd.texas.gov/gis/data", "bulk-download", "Source classification remains distinct from NLCD."),
    ("Annual NLCD", "USGS", "Annual National Land Cover Database", "https://www.usgs.gov/land-resources/national-land-cover-database", "bulk-download", "Source classification remains distinct from TPWD EMS."),
    ("NOAA Climate Normals", "NOAA NCEI", "Normals monthly", "https://www.ncei.noaa.gov/access/services/data/v1?dataset=normals-monthly", "http", "Use dataset=normals-monthly; normals-annual is rejected."),
    ("NOAA Atlas 14", "NOAA NWS", "Atlas 14 Volume 11 Texas grids", "https://hdsc.nws.noaa.gov/pub/hdsc/data/tx/", "bulk-download", ""),
    ("USDA NAIP", "USDA", "National Agriculture Imagery Program", "https://planetarycomputer.microsoft.com/api/stac/v1/collections/naip", "stac", "Microsoft Planetary Computer STAC is the anonymous path; AWS bucket is requester-pays."),
    ("TxGIO building footprints", "TxGIO", "Building footprints", "https://data.tnris.org/", "bulk-download", ""),
    ("TxGIO address points", "TxGIO", "Address points", "https://data.tnris.org/", "bulk-download", ""),
    ("US Census geocoder", "US Census", "Oneline address geocoder", "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress", "http", "benchmark=Public_AR_Current; no key."),
]


def main() -> None:
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        org = session.get(Organization, get_settings().dev_organization_id) or Organization(id=get_settings().dev_organization_id, name="SiteSense Demo")
        user = session.get(User, get_settings().dev_user_id) or User(id=get_settings().dev_user_id, email="demo@sitesense.local", name="Demo User")
        session.add_all([org, user])
        session.merge(OrganizationUser(organization_id=org.id, user_id=user.id, role="owner"))
        existing = {row.dataset_name for row in session.scalars(select(DataSource))}
        assert len(existing) == len(list(session.scalars(select(DataSource)))), "dataset_name must be unique"
        now = datetime.now(UTC)
        session.add_all(DataSource(name=n, agency=a, dataset_name=d, source_url=u, access_method=m, retrieved_at=now, notes=notes) for n, a, d, u, m, notes in SOURCES if d not in existing)
        session.commit()


if __name__ == "__main__":
    main()
