# SecRAG backend (FastAPI). Build context: ./backend
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; torch is installed CPU-only to keep the image small.
COPY requirements.txt ./
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

ENV PYTHONPATH=/app/src
EXPOSE 8000

# Run DB migrations, then start the API.
CMD ["sh", "-c", "alembic upgrade head && uvicorn rag_app.api.app:app --host 0.0.0.0 --port 8000"]
