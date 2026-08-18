import uuid
from datetime import date, datetime
from enum import StrEnum

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStage(StrEnum):
    queued = "queued"
    fetching = "fetching"
    processing = "processing"
    derivatives = "derivatives"
    complete = "complete"
    failed = "failed"
    partial = "partial"


class CategoryStatus(StrEnum):
    queued = "queued"
    fetching = "fetching"
    processing = "processing"
    complete = "complete"
    unavailable = "unavailable"
    failed = "failed"


class Confidence(StrEnum):
    high = "high"
    medium = "medium"
    low = "low"


class OpportunityStatus(StrEnum):
    suggested = "suggested"
    accepted = "accepted"
    edited = "edited"
    rejected = "rejected"


class TenantModel(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class OrganizationUser(Base):
    __tablename__ = "organization_users"
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(50), default="member")


class Client(TenantModel):
    __tablename__ = "clients"
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Project(TenantModel):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("clients.id"))


class Property(TenantModel):
    __tablename__ = "properties"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    address: Mapped[str] = mapped_column(String(500), nullable=False)
    geometry: Mapped[object | None] = mapped_column(Geometry("POLYGON", srid=4326))


class Parcel(TenantModel):
    __tablename__ = "parcels"
    property_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id"))
    county: Mapped[str] = mapped_column(String(100), nullable=False)
    appraisal_parcel_id: Mapped[str] = mapped_column(String(150), nullable=False)
    situs_address: Mapped[str | None] = mapped_column(String(500))
    legal_description: Mapped[str | None] = mapped_column(Text)
    appraisal_record_acres: Mapped[float | None] = mapped_column(Float)
    computed_acres: Mapped[float | None] = mapped_column(Float)
    raw_source_attributes: Mapped[dict[str, object] | None] = mapped_column(JSON)
    geometry: Mapped[object] = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)


class SiteAnalysis(TenantModel):
    __tablename__ = "site_analyses"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    parcel_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("parcels.id"))


class AnalysisCategory(TenantModel):
    __tablename__ = "analysis_categories"
    analysis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[CategoryStatus] = mapped_column(
        Enum(CategoryStatus, name="category_status"), default=CategoryStatus.queued, nullable=False
    )
    confidence: Mapped[Confidence | None] = mapped_column(Enum(Confidence, name="confidence"))
    confidence_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)


class AnalysisLayer(TenantModel):
    __tablename__ = "analysis_layers"
    analysis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    geometry: Mapped[object | None] = mapped_column(Geometry("GEOMETRY", srid=4326))
    object_store_key: Mapped[str | None] = mapped_column(Text)
    layer_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSON, default=dict, nullable=False)


class DerivedMetric(TenantModel):
    __tablename__ = "derived_metrics"
    analysis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))


class SoilUnit(TenantModel):
    __tablename__ = "soil_units"
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"))
    geometry: Mapped[object] = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)
    mukey: Mapped[str] = mapped_column(String(50), nullable=False)
    musym: Mapped[str | None] = mapped_column(String(50))
    map_unit_name: Mapped[str | None] = mapped_column(String(255))
    acres: Mapped[float | None] = mapped_column(Float)
    parcel_percent: Mapped[float | None] = mapped_column(Float)
    dominant_component_name: Mapped[str | None] = mapped_column(String(255))
    component_percent: Mapped[float | None] = mapped_column(Float)
    slope_low: Mapped[float | None] = mapped_column(Float)
    slope_representative: Mapped[float | None] = mapped_column(Float)
    slope_high: Mapped[float | None] = mapped_column(Float)
    drainage_class: Mapped[str | None] = mapped_column(String(100))
    hydrologic_group: Mapped[str | None] = mapped_column(String(20))
    available_water_storage: Mapped[float | None] = mapped_column(Float)
    ksat: Mapped[float | None] = mapped_column(Float)
    depth_to_restrictive_layer: Mapped[float | None] = mapped_column(Float)
    flooding_frequency: Mapped[str | None] = mapped_column(String(100))
    ponding_class: Mapped[str | None] = mapped_column(String(100))
    farmland_classification: Mapped[str | None] = mapped_column(String(150))


class EcologicalUnit(TenantModel):
    __tablename__ = "ecological_units"
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"))
    geometry: Mapped[object] = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)
    system_vegetation_type: Mapped[str | None] = mapped_column(String(255))
    source_classification_code: Mapped[str | None] = mapped_column(String(100))
    acres: Mapped[float | None] = mapped_column(Float)
    parcel_percent: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), nullable=False)


class Wetland(TenantModel):
    __tablename__ = "wetlands"
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"))
    geometry: Mapped[object] = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)
    nwi_attribute_code: Mapped[str | None] = mapped_column(String(100))
    wetland_type: Mapped[str | None] = mapped_column(String(255))
    acres: Mapped[float | None] = mapped_column(Float)
    intersects_parcel: Mapped[bool] = mapped_column(Boolean, nullable=False)


class FloodZone(TenantModel):
    __tablename__ = "flood_zones"
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"))
    geometry: Mapped[object] = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=False)
    zone_classification: Mapped[str | None] = mapped_column(String(100))
    source_discriminator: Mapped[str] = mapped_column(String(40), nullable=False)
    acres_intersected: Mapped[float | None] = mapped_column(Float)
    parcel_percent: Mapped[float | None] = mapped_column(Float)
    annual_chance: Mapped[str | None] = mapped_column(String(100))


class Well(TenantModel):
    __tablename__ = "wells"
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("site_analyses.id"))
    geometry: Mapped[object] = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    state_well_number: Mapped[str | None] = mapped_column(String(100))
    depth: Mapped[float | None] = mapped_column(Float)
    water_level: Mapped[float | None] = mapped_column(Float)
    aquifer: Mapped[str | None] = mapped_column(String(150))
    use_type: Mapped[str | None] = mapped_column(String(100))
    completion_date: Mapped[date | None] = mapped_column(Date)
    distance_to_parcel: Mapped[float | None] = mapped_column(Float)
    query_radius_miles: Mapped[int] = mapped_column(Integer, nullable=False)


class ClientGoal(TenantModel):
    __tablename__ = "client_goals"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    goal: Mapped[str] = mapped_column(String(80), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class Opportunity(TenantModel):
    __tablename__ = "opportunities"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_data_refs: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[OpportunityStatus] = mapped_column(
        Enum(OpportunityStatus, name="opportunity_status"), default=OpportunityStatus.suggested
    )
    confidence: Mapped[Confidence | None] = mapped_column(Enum(Confidence, name="opportunity_confidence"))
    requires_field_verification: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    geometry: Mapped[object | None] = mapped_column(Geometry("GEOMETRY", srid=4326))


class ProjectFeature(TenantModel):
    __tablename__ = "project_features"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    geometry: Mapped[object] = mapped_column(Geometry("GEOMETRY", srid=4326), nullable=False)


class PricebookItem(TenantModel):
    __tablename__ = "pricebook_items"
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    unit_price_low: Mapped[float] = mapped_column(Float, nullable=False)
    unit_price_high: Mapped[float] = mapped_column(Float, nullable=False)
    is_example_value: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ScopeItem(TenantModel):
    __tablename__ = "scope_items"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    pricebook_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("pricebook_items.id"))
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    geometry: Mapped[object | None] = mapped_column(Geometry("GEOMETRY", srid=4326))
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    unit_price_low: Mapped[float] = mapped_column(Float, nullable=False)
    unit_price_high: Mapped[float] = mapped_column(Float, nullable=False)
    fixed_cost: Mapped[float | None] = mapped_column(Float)
    estimated_total_low: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_total_high: Mapped[float] = mapped_column(Float, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Proposal(TenantModel):
    __tablename__ = "proposals"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)


class ProposalSection(TenantModel):
    __tablename__ = "proposal_sections"
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("proposals.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agency: Mapped[str] = mapped_column(String(255), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    access_method: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    license: Mapped[str | None] = mapped_column(Text)
    spatial_resolution: Mapped[str | None] = mapped_column(String(100))
    temporal_resolution: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)


class AnalysisSourceRef(Base):
    __tablename__ = "analysis_source_refs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), index=True, nullable=False
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id"), nullable=False)
    derived_table: Mapped[str] = mapped_column(String(80), nullable=False)
    derived_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class Job(TenantModel):
    __tablename__ = "jobs"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    stage: Mapped[JobStage] = mapped_column(
        Enum(JobStage, name="job_stage"), default=JobStage.queued, nullable=False
    )
    category_status: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_detail: Mapped[str | None] = mapped_column(Text)
