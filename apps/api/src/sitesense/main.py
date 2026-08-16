from fastapi import FastAPI

from sitesense.routers import analysis, projects, stubs

app = FastAPI(title="SiteSense API", version="0.1.0")
app.include_router(projects.router, prefix="/api")
app.include_router(analysis.router, prefix="/api")
app.include_router(stubs.router, prefix="/api")
app.include_router(stubs.project_router, prefix="/api")


@app.get("/healthz", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
