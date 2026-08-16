from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    client_id: UUID | None = None


class ProjectRead(ProjectCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    organization_id: UUID


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    project_id: UUID
    stage: str
    category_status: dict[str, str]
    error_detail: str | None = None


AnalyzeResponse = JobRead


class AnalyzeRequest(BaseModel):
    project_id: UUID


class AnalysisRead(BaseModel):
    status: str
    confidence: str
    confidence_reason: str


class ParcelCandidate(BaseModel):
    candidate_id: UUID
    county: str
    source_url: str
    source_feature_id: str
    parcel_id: str
    situs_address: str | None = None
    legal_description: str | None = None
    appraisal_acres: float | None = None
    computed_acres: float
    owner: str | None = None
    geometry: dict[str, object]
    raw_attributes: dict[str, object] = Field(default_factory=dict)
    distance_meters: float = 0
    contains_point: bool = False


class ParcelSearchResponse(BaseModel):
    candidates: list[ParcelCandidate]
    latitude: float
    longitude: float
    matched_address: str | None = None
    geocoder_failed: bool = False
    source_health: list[dict[str, str]] = Field(default_factory=list)
    disclaimer: str


class ParcelSearchRequest(BaseModel):
    address: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    buffer_meters: float = Field(default=1000, ge=0, le=5000)

    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class GoalCreate(BaseModel):
    goal: str
    notes: str | None = None


class FeatureCreate(BaseModel):
    name: str
    geometry: dict[str, object]


class NotImplementedResponse(BaseModel):
    detail: str
    code: str = "not_implemented"


class ParcelConfirmRequest(BaseModel):
    candidate: ParcelCandidate


class ConfirmedParcelRead(BaseModel):
    parcel_id: UUID
    project_id: UUID
    county: str
    appraisal_parcel_id: str
    situs_address: str | None = None
    legal_description: str | None = None
    appraisal_record_acres: float | None = None
    computed_acres: float | None = None
    geometry: dict[str, object]
    disclaimer: str


class GoalUpdate(BaseModel):
    goal: str
    notes: str | None = None


class FeatureUpdate(BaseModel):
    name: str | None = None
    geometry: dict[str, object] | None = None


class OpportunityUpdate(BaseModel):
    status: str | None = None
    description: str | None = None


class PricebookItemUpdate(BaseModel):
    unit_price_low: float | None = None
    unit_price_high: float | None = None
    is_example_value: bool | None = None


class ScopeUpdate(BaseModel):
    quantity: float | None = None
    notes: str | None = None


class ProposalUpdate(BaseModel):
    title: str | None = None
    notes: str | None = None
