# SecRAG backend (FastAPI). Build context: ./backend
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; torch is installed CPU-only (pinned, from its own index) to keep
# the image small, then the runtime pins from PyPI.
COPY requirements-torch.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-torch.txt \
    && pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

ENV PYTHONPATH=/app/src
EXPOSE 8000

# The API only. Migrations never run at container start (T11.2.5): locally the compose one-shot
# `migrate` runs them before the backend starts; on Azure the migration Job, started by CD
# before the apps are updated. The lifespan refuses to start on invalid settings or a
# master-key fingerprint mismatch (T11.2.2, T11.2.4).
CMD ["uvicorn", "rag_app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
