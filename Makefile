.PHONY: install build run clean

# Variables
PYTHON = python3
BUN = bun
BACKEND_DIR = backend
FRONTEND_DIR = frontend
STATIC_DIR = $(BACKEND_DIR)/static

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

# Run backend (with dev hook secret for local testing)
run:
	@echo "Starting server..."
	@echo "Hook Secret: dev_hook_secret_local_12345"
	PYTHONPATH=$(BACKEND_DIR) ARIA2C_HOOK_SECRET=dev_hook_secret_local_12345 ARIA2C_RPC_SECRET=1 uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Clean
clean:
	rm -rf $(STATIC_DIR)
	rm -rf $(FRONTEND_DIR)/out
	rm -rf $(FRONTEND_DIR)/.next
	find . -type d -name "__pycache__" -exec rm -rf {} +
