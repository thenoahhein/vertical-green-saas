FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv \
    && uv sync --extra dev
COPY apps ./apps
ENV PYTHONPATH=/app/apps/api/src:/app/apps/worker/src
CMD ["uv", "run", "celery", "-A", "sitesense_worker.tasks.celery_app", "worker", "--loglevel=INFO"]
