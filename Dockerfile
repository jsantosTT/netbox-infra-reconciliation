FROM python:3.12-slim

# Secrets are never baked in; pass .env at runtime with --env-file.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NBRECON_ARTIFACT_DIR=/var/lib/nbrecon/runs \
    NBRECON_RUN_HISTORY_DB=/var/lib/nbrecon/run-history.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 nbrecon \
    && mkdir -p /var/lib/nbrecon \
    && chown -R nbrecon:nbrecon /var/lib/nbrecon

USER nbrecon

# Dry-run is the default; apply requires an explicit approved plan file.
ENTRYPOINT ["nbrecon"]
CMD ["--help"]
