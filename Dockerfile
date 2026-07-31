# ============================================================================
# Stage 1: Build frontend
# ============================================================================
FROM oven/bun:1.3.11@sha256:0733e50325078969732ebe3b15ce4c4be5082f18c4ac1a0f0ca4839c2e4e42a7 AS frontend-builder

WORKDIR /app/frontend

# Install dependencies
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile

# Build static export
COPY frontend/ ./
RUN bun run build

# ============================================================================
# Stage 2: Runtime
# ============================================================================
FROM python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    && curl -sL "https://github.com/mcmilk/7-Zip-zstd/releases/download/v25.01-v1.5.7-R4/linux-gcc-x64.zip" -o /tmp/7z.zip \
    && unzip -q /tmp/7z.zip 7zz -d /usr/local/bin \
    && chmod +x /usr/local/bin/7zz \
    && rm /tmp/7z.zip \
    && apt-get purge -y unzip \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.8.12@sha256:f64ad69940b634e75d2e4d799eb5238066c5eeda49f76e782d4873c3d014ea33 /uv /usr/local/bin/uv

# Copy Python dependencies and install
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy backend code
COPY backend/ ./backend/

# Copy frontend build output
COPY --from=frontend-builder /app/frontend/out ./backend/static/

# Create directories for data persistence
RUN mkdir -p /app/backend/data /app/backend/downloads

# Environment variables
ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    ARIA2C_DATABASE_PATH=/app/backend/data/app.db \
    ARIA2C_DOWNLOAD_DIR=/app/backend/downloads \
    ARIA2C_HOST=0.0.0.0 \
    ARIA2C_PORT=8001

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${ARIA2C_PORT:-8001}/api/health || exit 1

# Run application
CMD ["sh", "-c", "uv run uvicorn app.main:app --host ${ARIA2C_HOST:-0.0.0.0} --port ${ARIA2C_PORT:-8001}"]
