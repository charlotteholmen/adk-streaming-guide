FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    APPS_CONFIG=/workspace/apps.yaml

WORKDIR /workspace

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY apps.yaml ./apps.yaml

RUN uv sync --frozen --no-dev

WORKDIR /workspace/app

CMD ["sh", "-c", "uv run --project .. uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
