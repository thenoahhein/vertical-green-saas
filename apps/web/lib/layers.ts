export type LayerDefinition = {
  id: string;
  label: string;
  enabled: boolean;
};

export const layerRegistry: LayerDefinition[] = [
  { id: "parcels", label: "Parcels", enabled: true },
  { id: "terrain", label: "Terrain", enabled: false },
  { id: "hydrology", label: "Hydrology", enabled: false },
];
