from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sitesense.config import get_settings
from sitesense.routers import analysis, parcels, projects, stubs

app = FastAPI(title="SiteSense API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(projects.router, prefix="/api")
app.include_router(analysis.router, prefix="/api")
app.include_router(parcels.router, prefix="/api")
app.include_router(parcels.project_router, prefix="/api")
app.include_router(stubs.router, prefix="/api")
app.include_router(stubs.project_router, prefix="/api")


@app.get("/healthz", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
