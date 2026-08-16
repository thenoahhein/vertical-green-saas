# SiteSense architecture

The platform is layered so source volatility does not leak into client proposals:

1. **External source adapters** fetch parcel, terrain, hydrology, soils, ecology, flood, wells, and climate data.
2. **Normalized derived site model** stores a property and parcel plus analysis, layers, metrics, provenance, and confidence.
3. **Deterministic analysis** computes measurements in a Texas-appropriate projected CRS (central Texas acreage uses EPSG:6578/6579 or an equal-area equivalent), never raw WGS84 degrees.
4. **Rule engine** turns metrics into opportunities.
5. **Narrative** explains evidence, confidence, and limitations.
6. **Proposal** maps accepted opportunities to scope, pricebook items, and a client-facing PDF.

The derived site model tree is:

`property → parcel → site_analysis → {terrain, hydrology, soils, ecology, flood, groundwater, climate} → layers + metrics + opportunities → project_features → scope → proposal`.

Every derived result is confidence-scored with a machine-readable reason and can be linked to one or more `data_sources` using `analysis_source_refs`. Partial category results are represented by status rather than hidden nulls.

## Adding a data source

Implement an adapter under the source-adapter layer, register its endpoint and access method in `data_sources`, normalize its output into the relevant spatial/model table, and create `analysis_source_refs` rows for every derived record. Add deterministic fixture tests; unit tests must not call government services.

## Parcel source adapter seam

Parcel discovery uses a source-adapter boundary rather than placing ArcGIS or
geocoder details in API routes. An adapter accepts a WGS84 point and query
parameters, then returns normalized parcel records with the originating
`DataSource` URL and raw source attributes. `ArcGISParcelAdapter` implements
the four county layer-0 services; the Census geocoder only supplies a
coordinate and is never treated as an authoritative parcel source.

Adapters use bounded HTTP timeouts. Remote errors are represented as
unavailable health results and surfaced with machine-readable reasons; they do
not escape as uncaught route errors. Confirmed parcel rows create
`analysis_source_refs` records linking the organization, normalized table, and
registered county source.

The live query path is county-specific ArcGIS FeatureServer layer 0. The
statewide TxGIO StratMap parcel service is bulk-download-only and its public
`/query` capability is disabled, so it is not used for live parcel search.

### County parcel field mappings

These mappings were inspected from each service's `?f=json` metadata. The
services currently share the following field names; Caldwell uses lowercase
`block` for an otherwise unmapped field, which remains in raw attributes.

| County | Parcel ID | Situs address fields | Legal description | Appraisal acres | Owner/display name |
| --- | --- | --- | --- | --- | --- |
| Bastrop | `prop_id_text` | `situs_num`, `situs_street_prefx`, `situs_street`, `situs_street_sufix`, `situs_city`, `situs_state`, `situs_zip` | `legal_desc`, `legal_desc2`, `legal_desc3` | `legal_acreage` | `file_as_name` |
| Lee | `prop_id_text` | same as Bastrop | same as Bastrop | `legal_acreage` | `file_as_name` |
| Fayette | `prop_id_text` | same as Bastrop | same as Bastrop | `legal_acreage` | `file_as_name` |
| Caldwell | `prop_id_text` | same as Bastrop | same as Bastrop | `legal_acreage` | `file_as_name` |

Missing fields remain null. Unmapped fields are retained in
`raw_source_attributes`; computed acreage is calculated from normalized
geometry and is deliberately separate from appraisal-record acreage.

To add a county, inspect and record its layer metadata, add a
`CountySource`/`ParcelFieldMapping`, register its `DataSource` in `seed.py`,
and provide a response fixture before enabling the adapter in production.
Fixtures drive normal tests. `scripts/live_source_health.py` is an explicit
opt-in check for upstream availability and is excluded from CI.

TxGIO StratMap statewide parcels is bulk-download-only: its public MapServer `/query` capability is disabled. Launch-county parcel adapters use the verified ArcGIS FeatureServers (layer 0) for Bastrop, Lee, Fayette, and Caldwell. USGS 3DEP uses public `prd-tnm` staged COGs / TNMAccess; USDA Soil Data Access requires POST; TWDB, FEMA NFHL, and USFWS NWI are registered as ArcGIS services.
