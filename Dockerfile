# The MAPP-D app: FastAPI + DBOS + the processes, and the mappd CLI (also the healthcheck).
FROM python:3.14-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
# pg_dump/pg_restore for `mappd db backup|restore`, matching the PostgreSQL 17 server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv PATH=/app/.venv/bin:$PATH \
    MAPPD_PROCESSES_DIR=/app/processes PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --locked --no-dev
COPY processes ./processes
COPY world.yaml ./world.yaml
ENV MAPPD_WORLD_PATH=/app/world.yaml
# The DBOS application version: the build argument (e.g. the git SHA) or a hash of the code, so a
# rebuilt image never replays an older version's journal (MAPPD_APP_VERSION still overrides it).
ARG MAPPD_APP_VERSION=
RUN v="$MAPPD_APP_VERSION"; \
    [ -n "$v" ] || v=$(find src processes -name '*.py' -type f | sort | xargs cat | sha256sum | cut -c1-12); \
    echo "$v" > /app/VERSION
ENV MAPPD_APP_VERSION_FILE=/app/VERSION

RUN useradd --uid 10001 --no-create-home mappd
USER mappd
EXPOSE 8000
CMD ["uvicorn", "--factory", "mappd.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
