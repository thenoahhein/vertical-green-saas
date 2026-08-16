import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ParcelCandidate } from "../lib/api";

const mocks = vi.hoisted(() => ({
  searchParcels: vi.fn(),
  createProject: vi.fn(),
  confirmParcel: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  confirmParcel: mocks.confirmParcel,
  createProject: mocks.createProject,
  searchParcels: mocks.searchParcels,
}));

vi.mock("maplibre-gl", () => {
  class MockMap {
    on() {
      return this;
    }
    addControl() {
      return this;
    }
    addSource() {}
    addLayer() {}
    getSource() {
      return undefined;
    }
    getLayer() {
      return undefined;
    }
    setLayoutProperty() {}
    flyTo() {}
    fitBounds() {}
    remove() {}
  }

  class MockBounds {
    extend() {
      return this;
    }
  }

  return {
    default: {
      Map: MockMap,
      NavigationControl: class {},
      LngLatBounds: MockBounds,
    },
  };
});

import PropertyWorkspace from "./PropertyWorkspace";

const candidate = (parcel_id: string): ParcelCandidate => ({
  candidate_id: parcel_id,
  county: "Bastrop",
  source_url: "https://example.test/source",
  source_feature_id: parcel_id,
  parcel_id,
  situs_address: `${parcel_id} CHESTNUT ST TX`,
  legal_description: `LOT ${parcel_id}`,
  appraisal_acres: 1,
  computed_acres: 1,
  owner: null,
  geometry: {
    type: "Polygon",
    coordinates: [[
      [-97, 30],
      [-97, 30.001],
      [-96.999, 30.001],
      [-96.999, 30],
      [-97, 30],
    ]],
  },
  raw_attributes: {},
  distance_meters: 0,
  contains_point: true,
});

describe("PropertyWorkspace selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.searchParcels.mockResolvedValue({
      candidates: [candidate("35585"), candidate("36249")],
      latitude: 30,
      longitude: -97,
      matched_address: "1311 Chestnut St, Bastrop, TX 78602",
      geocoder_failed: false,
      disclaimer: "Parcel disclaimer",
      source_health: [],
    });
    mocks.createProject.mockResolvedValue({ id: "project-1" });
    mocks.confirmParcel.mockResolvedValue({
      parcel_id: "confirmed",
      project_id: "project-1",
      county: "Bastrop",
      appraisal_parcel_id: "35585",
      situs_address: "35585 CHESTNUT ST TX",
      legal_description: "LOT 35585",
      appraisal_record_acres: 1,
      computed_acres: 1,
      geometry: candidate("35585").geometry,
      disclaimer: "Parcel disclaimer",
    });
  });

  it("keeps the clicked candidate as the confirmation target after hover", async () => {
    render(<PropertyWorkspace />);
    fireEvent.change(screen.getByPlaceholderText("Search a Central Texas address"), {
      target: { value: "1311 Chestnut St, Bastrop, TX 78602" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search parcels" }));

    await screen.findByText("Parcel ID: 35585");
    const rows = screen.getAllByRole("button");
    const firstCandidate = rows.find((row) => row.textContent?.includes("Parcel ID: 35585"));
    const secondCandidate = rows.find((row) => row.textContent?.includes("Parcel ID: 36249"));
    expect(firstCandidate).toBeDefined();
    expect(secondCandidate).toBeDefined();

    fireEvent.click(firstCandidate!);
    fireEvent.mouseEnter(secondCandidate!);

    const confirmButton = screen.getByRole("button", { name: "Confirm parcel 35585" });
    fireEvent.click(confirmButton);

    await waitFor(() => expect(mocks.confirmParcel).toHaveBeenCalledWith("project-1", expect.objectContaining({ parcel_id: "35585" })));
    expect(mocks.confirmParcel).not.toHaveBeenCalledWith("project-1", expect.objectContaining({ parcel_id: "36249" }));
  });
});
