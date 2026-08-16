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

TxGIO StratMap statewide parcels is bulk-download-only: its public MapServer `/query` capability is disabled. Launch-county parcel adapters use the verified ArcGIS FeatureServers (layer 0) for Bastrop, Lee, Fayette, and Caldwell. USGS 3DEP uses public `prd-tnm` staged COGs / TNMAccess; USDA Soil Data Access requires POST; TWDB, FEMA NFHL, and USFWS NWI are registered as ArcGIS services.
