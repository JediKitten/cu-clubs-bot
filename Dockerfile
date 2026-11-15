FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir /backups && chown app:app /backups
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
USER app
EXPOSE 8000
CMD ["cu-clubs", "api"]

FROM runtime AS selfservice
USER root
RUN apt-get update && apt-get install --no-install-recommends -y openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && groupmod --gid 10001 app \
    && usermod --uid 10001 --gid 10001 --shell /bin/sh app \
    && mkdir -p /run/sshd /data && chown -R app:app /app /backups /data \
    && usermod --password '*' app
COPY --chown=app:app deploy/selfservice.py deploy/ops.py deploy/sshd_config /app/deploy/
USER app
EXPOSE 2222
CMD ["python", "/app/deploy/selfservice.py"]

# Preserve the original image command for builds without an explicit target.
FROM runtime AS default
