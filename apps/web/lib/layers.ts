export type LayerDefinition = {
  id: string;
  label: string;
  enabled: boolean;
  available: boolean;
  mapLayerIds: string[];
};

export const layerRegistry: LayerDefinition[] = [
  { id: "parcels", label: "Parcels", enabled: true, available: true, mapLayerIds: ["parcel-fill", "parcel-outline"] },
  { id: "terrain", label: "Terrain", enabled: false, available: false, mapLayerIds: [] },
  { id: "hydrology", label: "Hydrology", enabled: false, available: false, mapLayerIds: [] },
];
