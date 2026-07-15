# syntax=docker/dockerfile:1
# Web app image: FastAPI + HTMX served by uvicorn. Data (the slim fundamentals store, jobs DB,
# logs) lives on a mounted volume, NOT in the image — see docker-compose.yml + docs/DEPLOY.md.
FROM python:3.12-slim

# uv (pinned) for fast, reproducible, lockfile-frozen installs
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# 1) dependencies only — this layer is cached until uv.lock changes
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra api --no-dev --no-install-project

# 2) the application (README is referenced by the project metadata)
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra api --no-dev

# run unprivileged as a fixed uid (10001). /data is the mount point — for a named volume it
# inherits this ownership; for a host bind mount, chown the host dir to 10001 (see docs/DEPLOY.md).
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown -R app:app /app /data
USER app

EXPOSE 8000
# /health is auth-exempt, so the probe works with the gate on
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"]
CMD ["uvicorn", "wheel_screener.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
