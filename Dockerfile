FROM python:3.12-slim AS runtime

ARG VCS_REF=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_NO_DEV=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

LABEL org.opencontainers.image.source="https://github.com/urasica/CCIM" \
      org.opencontainers.image.revision="${VCS_REF}"

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install only versions recorded in uv.lock. A stale lockfile fails the build.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
RUN uv sync --locked --no-dev --no-editable

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/live', timeout=2)"]

CMD ["uvicorn", "ccim.main:app", "--host", "0.0.0.0", "--port", "8080"]
