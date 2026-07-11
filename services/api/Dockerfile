FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY services/api/pyproject.toml services/api/uv.lock /app/services/api/
RUN cd /app/services/api && uv sync --frozen --no-dev --no-install-project

COPY services/api/README.md /app/services/api/README.md
COPY services/api/src /app/services/api/src
RUN cd /app/services/api && uv sync --frozen --no-dev

COPY alembic.ini /app/alembic.ini
COPY database /app/database
COPY data /app/data
COPY services/api/docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh
ENV PATH="/app/services/api/.venv/bin:${PATH}" \
    PYTHONPATH="/app/services/api/src"

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
