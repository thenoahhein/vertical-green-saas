FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv \
    && uv sync --extra dev \
    && uv run python -c "from whitebox.whitebox_tools import download_wbt; download_wbt(verbose=False)"
COPY apps ./apps
COPY seed.py .
ENV PYTHONPATH=/app/apps/api/src:/app/apps/worker/src
CMD ["uv", "run", "uvicorn", "sitesense.main:app", "--host", "0.0.0.0", "--port", "8000"]
