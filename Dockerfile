# syntax=docker/dockerfile:1
#
# One image for every Woxpas app process (API, pipeline worker, dreaming
# worker, MCP server). Compose picks the process via `command:`. The data
# services (Postgres / Qdrant / MinIO / ClamAV) use their own upstream images.
#
# Build:  docker compose build
# The first build compiles llama-cpp-python (local LLM) from source, so it
# needs the toolchain below and takes a few minutes. Subsequent builds are cached.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Native build deps: llama-cpp-python compiles llama.cpp (cmake + a C/C++
# toolchain); libgomp1 is the OpenMP runtime onnxruntime / CTranslate2 need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    # Model weights + every HF/cache write go to the mounted volume, so the
    # container can run with a read-only root filesystem.
    WOXPAS_MODELS_DIR=/var/woxpas/models \
    HF_HOME=/var/woxpas/models/hf \
    HF_HUB_CACHE=/var/woxpas/models/hf \
    XDG_CACHE_HOME=/var/woxpas/models/cache

# Resolve + install the whole workspace from the frozen lockfile. The package
# sources are copied first because the workspace members install editable.
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --all-packages --no-dev --frozen

# SQL migrations + infra config (used by some entry points at runtime).
COPY migrations ./migrations
COPY infra ./infra

# Run unprivileged; pre-create the writable model/cache dir.
RUN useradd --create-home --uid 10001 woxpas \
    && mkdir -p /var/woxpas/models \
    && chown -R woxpas:woxpas /var/woxpas /app
USER woxpas

# Default process; compose overrides per service (woxpas-worker, etc.).
CMD ["woxpas-api"]
