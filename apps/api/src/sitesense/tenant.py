from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Header, HTTPException
from sqlalchemy import Select, select

from sitesense.config import get_settings
from sitesense.db import get_db


@dataclass(frozen=True)
class CurrentOrg:
    organization_id: UUID
    user_id: UUID


async def current_org(authorization: str | None = Header(default=None)) -> CurrentOrg:
    settings = get_settings()
    if authorization != f"Bearer {settings.dev_bearer_token}":
        raise HTTPException(status_code=401, detail="Valid development bearer token required")
    return CurrentOrg(UUID(settings.dev_organization_id), UUID(settings.dev_user_id))


def scoped_query(model: Any, org: CurrentOrg) -> Select[Any]:
    """The single tenant boundary for organization-owned tables."""
    return select(model).where(model.organization_id == org.organization_id)


async def scoped_get(db: Any, model: Any, object_id: UUID, org: CurrentOrg) -> Any:
    result = await db.execute(
        scoped_query(model, org).where(model.id == object_id)
    )
    instance = result.scalar_one_or_none()
    if instance is None:
        raise HTTPException(status_code=404, detail=f"{model.__tablename__.title()} not found")
    return instance


__all__ = ["CurrentOrg", "current_org", "get_db", "scoped_get", "scoped_query"]
