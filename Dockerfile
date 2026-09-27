FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:0.10.6@sha256:2f2ccd27bbf953ec7a9e3153a4563705e41c852a5e1912b438fc44d88d6cb52c AS uv
FROM --platform=linux/amd64 docker.io/library/python:3.13.14-slim-bookworm@sha256:dd86541a59b252667f4c12f8b2ee17216de37dd65ac773bf097bef996fa78860 AS builder

ARG TARGETARCH
RUN test "$TARGETARCH" = "amd64" && python -c 'import platform,sys; assert platform.machine()=="x86_64"; assert sys.version_info[:3]==(3,13,14)'
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV PYTHONTZPATH="" UV_PROJECT_ENVIRONMENT=/app/.venv UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN /app/.venv/bin/python -m ops.testing.timezone_contract uv.lock && DJANGO_SETTINGS_MODULE=config.settings.base /app/.venv/bin/python manage.py collectstatic --noinput

FROM --platform=linux/amd64 docker.io/library/python:3.13.14-slim-bookworm@sha256:dd86541a59b252667f4c12f8b2ee17216de37dd65ac773bf097bef996fa78860 AS runtime

ARG TARGETARCH
ARG CLINIC_REVISION_SHA
ARG CLINIC_TREE_SHA
ARG CLINIC_SOURCE_MANIFEST_SHA256
ARG CLINIC_SOURCE_ENTRY_COUNT
RUN test "$TARGETARCH" = "amd64" && python -c 'import platform,sys; assert platform.machine()=="x86_64"; assert sys.version_info[:3]==(3,13,14)'
LABEL clinic.phase1a.image-kind="application" \
      org.opencontainers.image.revision="$CLINIC_REVISION_SHA" \
      clinic.phase1a.tree="$CLINIC_TREE_SHA" \
      clinic.phase1a.application-source-sha256="$CLINIC_SOURCE_MANIFEST_SHA256" \
      clinic.phase1a.application-source-entry-count="$CLINIC_SOURCE_ENTRY_COUNT" \
      clinic.phase1a.python-version="3.13.14" \
      clinic.phase1a.uv-version="0.10.6" \
      clinic.phase1a.tzdata-version="2026.3"
WORKDIR /app
ENV HOME=/tmp PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONTZPATH="" CLINIC_PROCESS_PURPOSE=web
COPY --from=builder --chown=10001:10001 /app /app
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/ops/container/entrypoint.sh"]
CMD ["gunicorn", "--config=/app/ops/container/gunicorn_no_proxy.py", "--bind=0.0.0.0:8000", "--workers=2", "--threads=4", "--timeout=30", "--graceful-timeout=30", "--keep-alive=5", "--max-requests=1000", "--max-requests-jitter=100", "--access-logfile=-", "--error-logfile=-", "config.wsgi:application"]

# Optional independent deploy unit. The final/default target remains WSGI.
FROM runtime AS realtime
ENV CLINIC_PROCESS_PURPOSE=realtime
CMD ["uvicorn", "config.asgi_realtime:application", "--host=0.0.0.0", "--port=8001", "--no-proxy-headers", "--no-access-log", "--lifespan=off", "--timeout-graceful-shutdown=5", "--limit-concurrency=1000"]

FROM runtime AS web
