.PHONY: install build run dev-front dev-back dev-aria2 clean docker-build docker-up docker-down docker-logs

# Variables
PYTHON = python3
BUN = bun
BACKEND_DIR = backend
FRONTEND_DIR = frontend
STATIC_DIR = $(BACKEND_DIR)/static
DEV_HOOK_SECRET = dev_hook_secret_local_12345
DEV_ARIA2_SECRET = 1

# Default target
all: build

# Install dependencies
install:
	@echo "Installing backend dependencies with uv..."
	uv sync
	@echo "Installing frontend dependencies with bun..."
	cd $(FRONTEND_DIR) && $(BUN) install

# Build frontend and move to backend static
build:
	@if [ ! -d "$(FRONTEND_DIR)/node_modules" ]; then \
		echo "Frontend deps missing, installing with bun..."; \
		cd $(FRONTEND_DIR) && $(BUN) install; \
	fi
	@echo "Building frontend..."
	cd $(FRONTEND_DIR) && $(BUN) run build
	@echo "Cleaning old static files..."
	rm -rf $(STATIC_DIR)
	mkdir -p $(STATIC_DIR)
	@echo "Moving frontend build to backend static directory..."
	cp -r $(FRONTEND_DIR)/out/* $(STATIC_DIR)/
	@echo "Build complete."

# Run backend with standard logs (compatibility target)
run:
	@echo "Starting backend with standard logs..."
	@echo "Hook Secret: $(DEV_HOOK_SECRET)"
	PYTHONPATH=$(BACKEND_DIR) ARIA2C_DEBUG=false ARIA2C_DEV_RESET_ADMIN_PASSWORD=false ARIA2C_HOOK_SECRET=$(DEV_HOOK_SECRET) ARIA2C_ARIA2_RPC_SECRET=$(DEV_ARIA2_SECRET) uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --log-level info

# Frontend development mode
# Use this when developing UI pages/components

dev-front:
	@echo "Building frontend static artifacts before dev..."
	@$(MAKE) build
	@echo "Starting frontend dev server on http://localhost:3000 ..."
	@echo "API requests will be sent to http://localhost:8000"
	cd $(FRONTEND_DIR) && NEXT_PUBLIC_API_BASE=http://localhost:8000 $(BUN) run dev

# Backend development mode (verbose logs)
# Difference from `run`: ARIA2C_DEBUG=true and uvicorn --log-level debug

dev-back:
	@echo "Starting backend dev mode with verbose logs..."
	@echo "Hook Secret: $(DEV_HOOK_SECRET)"
	@echo "Dev mode will reset admin password to default (123456) on each start."
	PYTHONPATH=$(BACKEND_DIR) ARIA2C_DEBUG=true ARIA2C_DEV_RESET_ADMIN_PASSWORD=true ARIA2C_HOOK_SECRET=$(DEV_HOOK_SECRET) ARIA2C_ARIA2_RPC_SECRET=$(DEV_ARIA2_SECRET) uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug

# Local aria2 backend for testing (foreground)
# Keep this running in another terminal while using dev-back/dev-front

dev-aria2:
	@echo "Starting local aria2 test backend..."
	bash $(BACKEND_DIR)/aria2/start.sh

# Clean
clean:
	rm -rf $(STATIC_DIR)
	rm -rf $(FRONTEND_DIR)/out
	rm -rf $(FRONTEND_DIR)/.next
	find . -type d -name "__pycache__" -exec rm -rf {} +

# Docker commands
docker-build:
	docker build -t aria2-controler .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f
