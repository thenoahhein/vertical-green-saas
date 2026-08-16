from sqlalchemy import Connection, update

from sitesense.models import Job, JobStage


def transition_job(
    connection: Connection,
    job_id: str,
    stage: JobStage,
    category_status: dict[str, str],
    error_detail: str | None = None,
) -> None:
    connection.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(stage=stage, category_status=category_status, error_detail=error_detail)
    )
