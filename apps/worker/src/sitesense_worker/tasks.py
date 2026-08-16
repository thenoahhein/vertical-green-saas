from celery import Celery
from sitesense.config import get_settings
from sitesense.jobs import transition_job
from sitesense.models import JobStage
from sqlalchemy import create_engine

settings = get_settings()
celery_app = Celery("sitesense", broker=settings.redis_url, backend=settings.redis_url)
engine = create_engine(settings.database_url)


def configure_database(database_url: str) -> None:
    global engine
    engine.dispose()
    engine = create_engine(database_url)


@celery_app.task(name="sitesense.noop_analysis")  # type: ignore[untyped-decorator]
def noop_analysis(job_id: str, outcome: str = "complete", error_detail: str | None = None) -> str:
    """Foundation task; later handoffs replace this with staged analysis."""
    with engine.begin() as connection:
        if outcome == "failed":
            transition_job(connection, job_id, JobStage.failed, {"foundation": "failed"}, error_detail or "Analysis failed.")
        elif outcome == "partial":
            transition_job(
                connection,
                job_id,
                JobStage.partial,
                {"terrain": "complete", "groundwater": "unavailable"},
                error_detail,
            )
        else:
            transition_job(connection, job_id, JobStage.complete, {"foundation": "complete"}, error_detail)
    return job_id


enqueue_analysis = noop_analysis
