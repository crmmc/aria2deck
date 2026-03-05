# ============================================================================
# Stage 1: Build frontend
# ============================================================================
FROM oven/bun:1 AS frontend-builder

WORKDIR /app/frontend

# Install dependencies
COPY frontend/package.json frontend/bun.lockb* ./
RUN bun install --frozen-lockfile

# Build static export
COPY frontend/ ./
RUN bun run build

# ============================================================================
# Stage 2: Runtime
# ============================================================================
FROM python:3.12-slim

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
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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
    ARIA2C_DOWNLOAD_DIR=/app/backend/downloads

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Run application
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
