# One worker on purpose: MODEL_RATE_LIMITER (20 rpm) is process-level, so a second worker would silently
# double the model rate. Scale with instances, not workers.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYDANTIC_AI_NO_BANNER=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY adpilot ./adpilot
COPY packs ./packs
COPY evals ./evals
# The bundled CSVs stay in the image so the container also runs credential-free in DuckDB mode.
COPY data/raw ./data/raw

RUN pip install --no-cache-dir ".[api,mcp]" \
    && useradd --create-home --uid 10001 adpilot \
    && chown -R adpilot:adpilot /app
USER adpilot

EXPOSE 8080
CMD ["sh", "-c", "uvicorn --factory adpilot.api.app:create_app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
