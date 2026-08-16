FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN pip install uv && uv sync --extra dev
COPY apps ./apps
COPY seed.py .
ENV PYTHONPATH=/app/apps/api/src:/app/apps/worker/src
CMD ["uv", "run", "uvicorn", "sitesense.main:app", "--host", "0.0.0.0", "--port", "8000"]
