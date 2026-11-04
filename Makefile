.PHONY: install build run dev-front dev-back dev-aria2 clean docker-build docker-up docker-down docker-logs \
        lint lint-back lint-front typecheck typecheck-back typecheck-front \
        test test-back test-front dup security check verify-v2-standalone

# Variables
PYTHON = python3
BUN = bun
BACKEND_DIR = backend/v2
FRONTEND_DIR = frontend/v2
STATIC_DIR = $(BACKEND_DIR)/static

# Default target
all: build

# Install dependencies
install:
	@echo "Installing backend dependencies with uv..."
	cd $(BACKEND_DIR) && uv sync
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
	cp -R $(FRONTEND_DIR)/dist/* $(STATIC_DIR)/
	@echo "Build complete."

# Run backend with standard logs (compatibility target)
run:
	@echo "Starting backend with standard logs..."
	cd $(BACKEND_DIR) && uv run uvicorn aria2deck.main:app --host 0.0.0.0 --port 8001 --reload --log-level info --no-access-log

# Frontend development mode
# Use this when developing UI pages/components

dev-front:
	@echo "Building frontend static artifacts before dev..."
	@$(MAKE) build
	@echo "Starting frontend dev server on http://localhost:3000 ..."
	@echo "API requests will be sent to http://localhost:8001"
	cd $(FRONTEND_DIR) && $(BUN) run dev

# Backend development mode (verbose logs)
# Difference from `run`: ARIA2DECK_DEBUG=true and uvicorn --log-level debug

dev-back:
	@echo "Starting V2 backend dev mode with verbose logs..."
	cd $(BACKEND_DIR) && ARIA2DECK_DEBUG=true uv run uvicorn aria2deck.main:app --host 0.0.0.0 --port 8001 --reload --log-level debug --no-access-log

# Local aria2 backend for testing (foreground)
# Keep this running in another terminal while using dev-back/dev-front

dev-aria2:
	@echo "Starting local aria2 test backend..."
	bash $(BACKEND_DIR)/aria2/start.sh

# ---- 本地质量工具链（提交前跑一次 `make check`）----

lint-back:
	@echo "Linting backend (ruff)..."
	cd $(BACKEND_DIR) && uv run ruff check src tests

lint-front:
	@echo "Linting frontend (eslint)..."
	cd $(FRONTEND_DIR) && $(BUN) run lint

lint: lint-back lint-front

typecheck-back:
	@echo "Type checking backend (mypy)..."
	cd $(BACKEND_DIR) && uv run mypy src

typecheck-front:
	@echo "Type checking frontend (tsc)..."
	cd $(FRONTEND_DIR) && $(BUN) run typecheck

typecheck: typecheck-back typecheck-front

test-back:
	@echo "Testing backend (pytest + coverage 95% gate)..."
	# 显式传 fail-under/branch，确保 V2 覆盖率门禁始终生效
	cd $(BACKEND_DIR) && uv run pytest --cov=aria2deck --cov-branch --cov-fail-under=95 --cov-report=term-missing

test-front:
	@echo "Testing frontend (jest + coverage 95% gate)..."
	cd $(FRONTEND_DIR) && $(BUN) run test -- --runInBand --coverage

test: test-back test-front

# 重复代码检测（信息参考，不参与门禁）
dup:
	@echo "Detecting duplicated code (jscpd)..."
	bunx jscpd $(BACKEND_DIR)/src $(FRONTEND_DIR)/src --min-tokens 70

# 深度安全扫描（首次运行需联网拉取规则集）
security:
	uvx semgrep scan --config auto --error $(BACKEND_DIR)/src $(FRONTEND_DIR)/src

# 在不含 V1 的临时目录重新安装、测试、构建并启动 V2
verify-v2-standalone:
	bash scripts/verify-v2-standalone.sh

# 提交前一键门禁：lint + 类型检查 + 测试（含覆盖率门禁）
check: lint typecheck test
	@echo "All quality checks passed ✅"

# Clean
clean:
	rm -rf $(STATIC_DIR)
	rm -rf $(FRONTEND_DIR)/dist
	rm -rf $(FRONTEND_DIR)/coverage
	find $(BACKEND_DIR)/src $(BACKEND_DIR)/tests -type d -name "__pycache__" -exec rm -rf {} +

# Docker commands
docker-build:
	docker build -t aria2-controler .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f
