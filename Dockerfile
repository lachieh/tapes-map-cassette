# syntax=docker/dockerfile:1
ARG PYTHON=python:3.13.18-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG UV=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336

FROM --platform=$BUILDPLATFORM ${UV} AS uv-native
FROM ${UV} AS uv-target

# Tests run once, natively: under emulation numba's JIT makes them take many minutes.
FROM --platform=$BUILDPLATFORM ${PYTHON} AS test
COPY --from=uv-native /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-install-project
COPY cassette.toml provision.sql ./
COPY tapes_map/ tapes_map/
COPY tests/ tests/
ARG VERSION=0.0.0
RUN sed -i "s/0\.0\.0/${VERSION}/g" cassette.toml && uv run --no-sync pytest -q

FROM ${PYTHON} AS build
COPY --from=uv-target /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY --from=test /src/cassette.toml ./
COPY --from=test /src/tapes_map/ tapes_map/
RUN python -m compileall -q tapes_map

FROM ${PYTHON}
# numba writes its JIT cache next to the package unless pointed elsewhere; the root filesystem is read-only.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NUMBA_CACHE_DIR=/tmp/numba \
    CASSETTE_LISTEN=0.0.0.0:9997
WORKDIR /app
COPY --from=build /app /app
USER 65532:65532
EXPOSE 9997
CMD ["python", "-m", "tapes_map"]
