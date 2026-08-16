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


class ParcelSearchRequest(BaseModel):
    address: str


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
    parcel_id: UUID


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
