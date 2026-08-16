from uuid import UUID

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from sitesense.schemas import (
    FeatureUpdate,
    GoalUpdate,
    NotImplementedResponse,
    OpportunityUpdate,
    ParcelConfirmRequest,
    PricebookItemUpdate,
    ProposalUpdate,
    ScopeUpdate,
)

router = APIRouter(tags=["planned"])
project_router = APIRouter(prefix="/projects/{project_id}", tags=["planned"])


def planned() -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_501_NOT_IMPLEMENTED, content={"detail": "This endpoint is planned for a later handoff.", "code": "not_implemented"})


@router.get("/parcel-search", response_model=NotImplementedResponse, status_code=501)
def parcel_search_get() -> JSONResponse:
    return planned()


@router.post("/parcel-search", response_model=NotImplementedResponse, status_code=501)
def parcel_search_post() -> JSONResponse:
    return planned()


@router.post("/parcel/confirm", response_model=NotImplementedResponse, status_code=501)
def parcel_confirm(payload: ParcelConfirmRequest) -> JSONResponse:
    return planned()


@router.get("/pricebook", response_model=NotImplementedResponse, status_code=501)
@router.post("/pricebook", response_model=NotImplementedResponse, status_code=501)
def pricebook() -> JSONResponse:
    return planned()


@router.patch("/features/{feature_id}", response_model=NotImplementedResponse, status_code=501)
def update_feature_top_level(feature_id: UUID, payload: FeatureUpdate) -> JSONResponse:
    return planned()


@router.delete("/features/{feature_id}", response_model=NotImplementedResponse, status_code=501)
def delete_feature_top_level(feature_id: UUID) -> JSONResponse:
    return planned()


@router.patch("/opportunities/{opportunity_id}", response_model=NotImplementedResponse, status_code=501)
def update_opportunity_top_level(opportunity_id: UUID, payload: OpportunityUpdate) -> JSONResponse:
    return planned()


@router.patch("/pricebook/items/{item_id}", response_model=NotImplementedResponse, status_code=501)
def update_pricebook(item_id: UUID, payload: PricebookItemUpdate) -> JSONResponse:
    return planned()


@router.patch("/scope/{scope_id}", response_model=NotImplementedResponse, status_code=501)
def update_scope(scope_id: UUID, payload: ScopeUpdate) -> JSONResponse:
    return planned()


@router.patch("/proposals/{proposal_id}", response_model=NotImplementedResponse, status_code=501)
def update_proposal(proposal_id: UUID, payload: ProposalUpdate) -> JSONResponse:
    return planned()


@router.post("/proposals/{proposal_id}/publish", response_model=NotImplementedResponse, status_code=501)
def publish_proposal(proposal_id: UUID) -> JSONResponse:
    return planned()


@router.post("/proposals/{proposal_id}/pdf", response_model=NotImplementedResponse, status_code=501)
def proposal_pdf(proposal_id: UUID) -> JSONResponse:
    return planned()


@project_router.get("/layers", response_model=NotImplementedResponse, status_code=501)
def layers() -> JSONResponse:
    return planned()


@project_router.get("/metrics", response_model=NotImplementedResponse, status_code=501)
def metrics() -> JSONResponse:
    return planned()


@project_router.post("/goals", response_model=NotImplementedResponse, status_code=501)
def create_goal(project_id: UUID, payload: GoalUpdate) -> JSONResponse:
    return planned()


@project_router.get("/features", response_model=NotImplementedResponse, status_code=501)
def list_features(project_id: UUID) -> JSONResponse:
    return planned()


@project_router.get("/opportunities", response_model=NotImplementedResponse, status_code=501)
def list_opportunities(project_id: UUID) -> JSONResponse:
    return planned()


@project_router.get("/scope", response_model=NotImplementedResponse, status_code=501)
def list_scope(project_id: UUID) -> JSONResponse:
    return planned()


@project_router.get("/proposal", response_model=NotImplementedResponse, status_code=501)
def get_proposal(project_id: UUID) -> JSONResponse:
    return planned()


@project_router.get("/parcel", response_model=NotImplementedResponse, status_code=501)
def get_project_parcel(project_id: UUID) -> JSONResponse:
    return planned()


@project_router.patch("/features/{feature_id}", response_model=NotImplementedResponse, status_code=501)
def update_feature(feature_id: UUID, payload: FeatureUpdate) -> JSONResponse:
    return planned()
