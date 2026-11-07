# syntax=docker/dockerfile:1.7
# Dev container for desakota.
#
# Hand-rolled on python:3.14-slim-bookworm because the native geo stack
# (GDAL / GEOS / PROJ) is apt-installed, and we only need one Python version.
# uv is copied from its upstream image — avoids a curl|sh step
# and keeps the binary pinned to a Debian-matching build.
#
# Single-stage on purpose: build tools are small compared to GDAL's shared
# libs and this is a dev image, not a production artifact.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.14-slim-bookworm

# UV_LINK_MODE=copy sidesteps hardlink warnings when /opt/venv is a named
# volume (as in docker-compose.yml) and /root/.cache is on a different
# filesystem (common on WSL / Docker Desktop on Windows).
# UV_PYTHON_DOWNLOADS=never pins us to the apt-installed interpreter so uv
# doesn't shadow it with a managed build.
# UV_COMPILE_BYTECODE=1 turns .pyc compilation on at install time —
# cheap at build, saves a startup round on first import.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DESAKOTA_DATA_DIR=/workspace/data

# Native deps for the geo stack:
#   - gdal-bin / libgdal-dev: rasterio, pyogrio (geopandas I/O engine)
#   - libgeos-dev: shapely 2
#   - libproj-dev / proj-bin / proj-data: pyproj + its CRS grids
#   - libspatialindex-dev: rtree (geopandas sjoin fallback)
#   - build-essential: a few 3.14 wheels still occasionally compile
#   - git, curl: fetchers, debugging
# apt lists are purged after install to keep the image slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
        proj-bin \
        proj-data \
        libspatialindex-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv binary from upstream, no curl|sh.
COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /workspace

# Install deps first so the layer caches across code edits.
# Only the lock + manifest go in; source is bind-mounted at runtime so
# edits reload live. --frozen fails if uv.lock is stale instead of silently
# re-resolving — lockfile truth matters here.
#
# BuildKit cache mount on /root/.cache/uv speeds up repeat builds; harmless
# when the daemon doesn't support BuildKit (pre-v23, Docker Desktop on very
# old Windows), but every supported version does. --frozen + --no-install-project
# means we install the locked dependency set and skip installing desakota itself
# (the source lands via bind-mount at runtime).
COPY pyproject.toml uv.lock .python-version README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Default to an interactive shell; compose services override this.
CMD ["/bin/bash"]
