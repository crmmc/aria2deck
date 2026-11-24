# ============================================================================
# Stage 1: Build frontend
# ============================================================================
FROM oven/bun:1.3.11@sha256:0733e50325078969732ebe3b15ce4c4be5082f18c4ac1a0f0ca4839c2e4e42a7 AS frontend-builder

WORKDIR /app/frontend/v2

# Install dependencies
COPY frontend/v2/package.json frontend/v2/bun.lock ./
RUN bun install --frozen-lockfile

# Build static export
COPY frontend/v2/ ./
RUN bun run build

# ============================================================================
# Stage 2: Runtime
# ============================================================================
FROM python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app/backend/v2

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    && curl --fail --show-error --location --proto '=https' --tlsv1.2 "https://github.com/mcmilk/7-Zip-zstd/releases/download/v25.01-v1.5.7-R4/linux-gcc-x64.zip" -o /tmp/7z.zip \
    && echo "b7526802535bf98d6268ce1960de7e36cf8ed6b4004c9ba3ac09db9e14d9a20d  /tmp/7z.zip" | sha256sum -c - \
    && unzip -q /tmp/7z.zip 7zz -d /usr/local/bin \
    && chmod +x /usr/local/bin/7zz \
    && rm /tmp/7z.zip \
    && apt-get purge -y unzip \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.8.12@sha256:f64ad69940b634e75d2e4d799eb5238066c5eeda49f76e782d4873c3d014ea33 /uv /usr/local/bin/uv

# Copy Python dependencies and install
COPY backend/v2/pyproject.toml backend/v2/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy backend code
COPY backend/v2/src ./src/

# Copy frontend build output
COPY --from=frontend-builder /app/frontend/v2/dist ./static/

# Create directories for data persistence
RUN mkdir -p /app/backend/data /app/backend/downloads

# Environment variables
ENV PYTHONPATH=/app/backend/v2/src \
    PYTHONUNBUFFERED=1 \
    ARIA2DECK_DATABASE_PATH=/app/backend/data/v2.db \
    ARIA2DECK_DOWNLOAD_DIR=/app/backend/downloads \
    ARIA2DECK_HOST=0.0.0.0 \
    ARIA2DECK_PORT=8001

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${ARIA2DECK_PORT:-8001}/api/health/ready || exit 1

# Run application（访问日志交给 uvicorn 默认输出，由 docker json-file 轮转）
CMD ["sh", "-c", "uv run --no-sync uvicorn aria2deck.main:app --host ${ARIA2DECK_HOST:-0.0.0.0} --port ${ARIA2DECK_PORT:-8001}"]
