FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 dockyard \
    && mkdir /data && chown dockyard:dockyard /data
COPY --from=build /app/.venv /app/.venv
# The git tag of the release, shown beside the logo: the Release workflow gives it. Empty in any
# other build, which Dockyard shows as dev.
ARG DOCKYARD_RELEASE=""
ENV DOCKYARD_RELEASE=$DOCKYARD_RELEASE
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DOCKYARD_DATA_DIR=/data \
    DOCKYARD_LISTEN_HOST=0.0.0.0
USER dockyard
EXPOSE 8000 8443
CMD ["python", "-m", "dockyard"]
