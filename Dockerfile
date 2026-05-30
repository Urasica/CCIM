FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv 설치
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# 의존성 먼저 (캐시 레이어 활용)
COPY pyproject.toml ./
COPY src/ccim/__init__.py src/ccim/__init__.py
RUN uv sync --frozen --no-dev || uv sync --no-dev

# 소스
COPY src ./src
COPY migrations ./migrations

EXPOSE 8080
CMD ["uv", "run", "uvicorn", "ccim.main:app", "--host", "0.0.0.0", "--port", "8080"]
