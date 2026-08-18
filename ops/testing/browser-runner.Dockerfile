FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:0.10.6@sha256:2f2ccd27bbf953ec7a9e3153a4563705e41c852a5e1912b438fc44d88d6cb52c AS uv
FROM --platform=linux/amd64 mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69 AS runtime

ARG TARGETARCH
ARG CLINIC_REVISION_SHA
ARG CLINIC_TREE_SHA
ARG CLINIC_SOURCE_MANIFEST_SHA256
ARG CLINIC_SOURCE_ENTRY_COUNT
ARG CLINIC_AVAILABLE_SUITE_IDS
RUN test "$TARGETARCH" = "amd64" && test "$(dpkg --print-architecture)" = "amd64"
RUN apt-get update && apt-get install -y --no-install-recommends libnss3-tools=2:3.98-1ubuntu0.2 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /bin/
WORKDIR /runner
ENV HOME=/tmp PATH="/runner/.venv/bin:$PATH" PLAYWRIGHT_VERSION=1.61.0 PYTHONDONTWRITEBYTECODE=1 PYTHONTZPATH="" UV_PROJECT_ENVIRONMENT=/runner/.venv UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group browser --no-install-project
COPY --chown=10001:10001 . .
RUN groupadd --gid 10001 clinic && useradd --uid 10001 --gid 10001 --no-create-home clinic && install -d -o 10001 -g 10001 -m 0700 /tmp/clinic-browser
LABEL clinic.phase1a.image-kind="browser-runner" \
      org.opencontainers.image.revision="$CLINIC_REVISION_SHA" \
      clinic.phase1a.tree="$CLINIC_TREE_SHA" \
      clinic.phase1a.runner-source-sha256="$CLINIC_SOURCE_MANIFEST_SHA256" \
      clinic.phase1a.runner-source-entry-count="$CLINIC_SOURCE_ENTRY_COUNT" \
      clinic.phase1a.available-suite-ids="$CLINIC_AVAILABLE_SUITE_IDS"
USER 10001:10001
ENTRYPOINT ["python", "-m", "ops.testing.browser_session"]
CMD ["hold"]
