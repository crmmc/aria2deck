.PHONY: install build run dev-front dev-back dev-aria2 clean docker-build docker-up docker-down docker-logs \
        lint lint-back lint-front typecheck typecheck-back typecheck-front \
        test test-back test-front dup security check

# Variables
PYTHON = python3
BUN = bun
BACKEND_DIR = backend
FRONTEND_DIR = frontend
STATIC_DIR = $(BACKEND_DIR)/static
DEV_ARIA2_SECRET = 1
DEV_BACK_DEBUG = true
DEV_BACK_RESET_ADMIN_PASSWORD = true

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
	PYTHONPATH=$(BACKEND_DIR) ARIA2C_DEBUG=false ARIA2C_DEV_RESET_ADMIN_PASSWORD=false ARIA2C_ARIA2_RPC_SECRET=$(DEV_ARIA2_SECRET) uv run uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload --log-level info --no-access-log

# Frontend development mode
# Use this when developing UI pages/components

dev-front:
	@echo "Building frontend static artifacts before dev..."
	@$(MAKE) build
	@echo "Starting frontend dev server on http://localhost:3000 ..."
	@echo "API requests will be sent to http://localhost:8001"
	cd $(FRONTEND_DIR) && NEXT_PUBLIC_API_BASE=http://localhost:8001 $(BUN) run dev

# Backend development mode (verbose logs)
# Difference from `run`: ARIA2C_DEBUG=true and uvicorn --log-level debug

dev-back:
	@echo "Starting backend dev mode with verbose logs..."
	@echo "ARIA2C_DEBUG=$(DEV_BACK_DEBUG)"
	@echo "ARIA2C_DEV_RESET_ADMIN_PASSWORD=$(DEV_BACK_RESET_ADMIN_PASSWORD)"
	@echo "Dev mode will reset the admin password from ARIA2DECK_INITIAL_ADMIN_PASSWORD when ARIA2C_DEV_RESET_ADMIN_PASSWORD=true."
	PYTHONPATH=$(BACKEND_DIR) ARIA2C_DEBUG=$(DEV_BACK_DEBUG) ARIA2C_DEV_RESET_ADMIN_PASSWORD=$(DEV_BACK_RESET_ADMIN_PASSWORD) ARIA2C_ARIA2_RPC_SECRET=$(DEV_ARIA2_SECRET) uv run uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload --log-level debug --no-access-log

# Local aria2 backend for testing (foreground)
# Keep this running in another terminal while using dev-back/dev-front

dev-aria2:
	@echo "Starting local aria2 test backend..."
	bash $(BACKEND_DIR)/aria2/start.sh

# ---- 本地质量工具链（提交前跑一次 `make check`）----

lint-back:
	@echo "Linting backend (ruff)..."
	uv run ruff check $(BACKEND_DIR)/app $(BACKEND_DIR)/tests

lint-front:
	@echo "Linting frontend (eslint)..."
	cd $(FRONTEND_DIR) && $(BUN) run lint

lint: lint-back lint-front

typecheck-back:
	@echo "Type checking backend (mypy)..."
	cd $(BACKEND_DIR) && uv run mypy app

typecheck-front:
	@echo "Type checking frontend (tsc)..."
	cd $(FRONTEND_DIR) && $(BUN) run typecheck

typecheck: typecheck-back typecheck-front

test-back:
	@echo "Testing backend (pytest + coverage 95% gate)..."
	# 显式传 fail-under/branch：coverage 从 backend/ 运行时读不到仓库根 pyproject.toml，
	# 隐式依赖配置会让门禁静默失效
	cd $(BACKEND_DIR) && uv run pytest --cov=app --cov-branch --cov-fail-under=95 --cov-report=term-missing

test-front:
	@echo "Testing frontend (jest + coverage 95% gate)..."
	cd $(FRONTEND_DIR) && $(BUN) run test -- --runInBand --coverage

test: test-back test-front

# 重复代码检测（信息参考，不参与门禁）
dup:
	@echo "Detecting duplicated code (jscpd)..."
	bunx jscpd $(BACKEND_DIR)/app $(FRONTEND_DIR)/app $(FRONTEND_DIR)/components $(FRONTEND_DIR)/hooks $(FRONTEND_DIR)/lib --min-tokens 70

# 深度安全扫描（首次运行需联网拉取规则集）
security:
	uvx semgrep scan --config auto --error $(BACKEND_DIR)/app $(FRONTEND_DIR)/app $(FRONTEND_DIR)/components $(FRONTEND_DIR)/hooks $(FRONTEND_DIR)/lib

# 提交前一键门禁：lint + 类型检查 + 测试（含覆盖率门禁）
check: lint typecheck test
	@echo "All quality checks passed ✅"

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
