#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

for command_name in uv bun python3 make tar aria2c; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        printf '缺少验收命令: %s\n' "${command_name}" >&2
        exit 1
    fi
done

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/aria2deck-v2-standalone.XXXXXX")"
APP_LOG="${WORK_DIR}/uvicorn.log"
ARIA2_LOG="${WORK_DIR}/aria2.log"
APP_PID=""
ARIA2_PID=""

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "${APP_PID}" ]] && kill -0 "${APP_PID}" 2>/dev/null; then
        kill "${APP_PID}"
        wait "${APP_PID}" || true
    fi
    if [[ -n "${ARIA2_PID}" ]] && kill -0 "${ARIA2_PID}" 2>/dev/null; then
        kill "${ARIA2_PID}"
        wait "${ARIA2_PID}" || true
    fi
    if [[ "${status}" -ne 0 ]]; then
        if [[ -f "${APP_LOG}" ]]; then
            printf '\nV2 启动日志:\n' >&2
            cat "${APP_LOG}" >&2
        fi
        if [[ -f "${ARIA2_LOG}" ]]; then
            printf '\naria2 启动日志:\n' >&2
            cat "${ARIA2_LOG}" >&2
        fi
    fi
    rm -rf -- "${WORK_DIR}"
    exit "${status}"
}
trap cleanup EXIT

printf '复制不包含 V1 的独立验收目录...\n'
mkdir -p "${WORK_DIR}/backend" "${WORK_DIR}/frontend"
tar -C "${SOURCE_ROOT}" \
    --exclude='backend/v2/.venv' \
    --exclude='backend/v2/.coverage' \
    --exclude='backend/v2/.mypy_cache' \
    --exclude='backend/v2/.pytest_cache' \
    --exclude='backend/v2/.ruff_cache' \
    --exclude='backend/v2/aria2/runtime' \
    --exclude='backend/v2/static' \
    --exclude='frontend/v2/node_modules' \
    --exclude='frontend/v2/coverage' \
    --exclude='frontend/v2/dist' \
    -cf - backend/v2 frontend/v2 | tar -C "${WORK_DIR}" -xf -

for root_file in Makefile Dockerfile docker-compose.yml .dockerignore pytest.ini; do
    cp "${SOURCE_ROOT}/${root_file}" "${WORK_DIR}/${root_file}"
done

mkdir -p "${WORK_DIR}/scripts"
cp "${SOURCE_ROOT}/scripts/verify-v2-standalone.sh" "${WORK_DIR}/scripts/"

[[ ! -e "${WORK_DIR}/backend/v1" ]]
[[ ! -e "${WORK_DIR}/frontend/v1" ]]
[[ ! -e "${WORK_DIR}/backend/v2/.venv" ]]
[[ ! -e "${WORK_DIR}/frontend/v2/node_modules" ]]

printf '安装并测试 V2 后端...\n'
(
    cd "${WORK_DIR}/backend/v2"
    uv sync --frozen
    uv run pytest -q
)

printf '安装并测试 V2 前端...\n'
(
    cd "${WORK_DIR}/frontend/v2"
    bun install --frozen-lockfile
    bun run test -- --runInBand
)

printf '构建 V2 前端并部署到 V2 后端...\n'
make -C "${WORK_DIR}" build

read -r APP_PORT ARIA2_PORT < <(
    python3 - <<'PY'
import socket

with socket.socket() as app_socket, socket.socket() as aria2_socket:
    app_socket.bind(("127.0.0.1", 0))
    aria2_socket.bind(("127.0.0.1", 0))
    print(app_socket.getsockname()[1], aria2_socket.getsockname()[1])
PY
)

read -r TEST_SHARE_SECRET TEST_CREDENTIAL_PEPPER TEST_INITIAL_PASSWORD TEST_ARIA2_SECRET < <(
    python3 - <<'PY'
import secrets

print(
    secrets.token_urlsafe(48),
    secrets.token_urlsafe(48),
    secrets.token_urlsafe(24),
    secrets.token_urlsafe(32),
)
PY
)
export ARIA2DECK_SHARE_JWT_SECRET="${TEST_SHARE_SECRET}"
export ARIA2DECK_CREDENTIAL_PEPPER="${TEST_CREDENTIAL_PEPPER}"
export ARIA2DECK_INITIAL_ADMIN_PASSWORD="${TEST_INITIAL_PASSWORD}"
export ARIA2DECK_DATABASE_PATH="${WORK_DIR}/data/app/v2.db"
export ARIA2DECK_DOWNLOAD_DIR="${WORK_DIR}/data/downloads"
export ARIA2DECK_STATIC_DIR="${WORK_DIR}/backend/v2/static"
export ARIA2DECK_INTERNAL_BASE_URL="http://127.0.0.1:${APP_PORT}"
export ARIA2DECK_ARIA2_RPC_URL="http://127.0.0.1:${ARIA2_PORT}/jsonrpc"
export ARIA2DECK_ARIA2_RPC_SECRET="${TEST_ARIA2_SECRET}"
export ARIA2DECK_ARIA2_RPC_PORT="${ARIA2_PORT}"
export ARIA2DECK_ARIA2_RUNTIME_DIR="${WORK_DIR}/data/aria2"

printf '启动 aria2 并等待 RPC 就绪...\n'
(
    cd "${WORK_DIR}/backend/v2"
    exec bash aria2/start.sh
) >"${ARIA2_LOG}" 2>&1 &
ARIA2_PID=$!

ARIA2_RPC_URL="http://127.0.0.1:${ARIA2_PORT}/jsonrpc" \
    ARIA2_RPC_SECRET="${TEST_ARIA2_SECRET}" \
    python3 - <<'PY'
import json
import os
import time
from urllib.request import Request, urlopen

payload = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": "ready",
        "method": "aria2.getVersion",
        "params": [f"token:{os.environ['ARIA2_RPC_SECRET']}"],
    }
).encode("utf-8")
for _ in range(60):
    try:
        request = Request(
            os.environ["ARIA2_RPC_URL"],
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=1) as response:
            body = json.load(response)
        assert "result" in body
        break
    except (AssertionError, OSError, json.JSONDecodeError):
        time.sleep(0.25)
else:
    raise SystemExit("aria2 RPC 未在 15 秒内就绪")
PY

printf '启动 V2 并检查 health、首页与初始管理员登录...\n'
(
    cd "${WORK_DIR}/backend/v2"
    exec uv run --no-sync uvicorn aria2deck.main:app \
        --host 127.0.0.1 --port "${APP_PORT}" --no-access-log
) >"${APP_LOG}" 2>&1 &
APP_PID=$!

APP_BASE_URL="http://127.0.0.1:${APP_PORT}" python3 - <<'PY'
import hashlib
import json
import os
import time
from urllib.request import Request, urlopen

base_url = os.environ["APP_BASE_URL"]
for _ in range(60):
    try:
        with urlopen(f"{base_url}/api/health/ready", timeout=1) as response:
            assert response.status == 200
            assert json.load(response) == {"ok": True}
        break
    except (AssertionError, OSError, json.JSONDecodeError):
        time.sleep(0.25)
else:
    raise SystemExit("V2 服务未在 15 秒内就绪")

with urlopen(f"{base_url}/", timeout=2) as response:
    assert response.status == 200
    html = response.read().decode("utf-8")
assert '<html lang="zh-CN">' in html
assert '<div id="root"></div>' in html
assert "/assets/" in html

salt = hashlib.sha256(b"admin").hexdigest().encode("ascii")
password_hash = hashlib.pbkdf2_hmac(
    "sha256",
    os.environ["ARIA2DECK_INITIAL_ADMIN_PASSWORD"].encode("utf-8"),
    salt,
    10000,
).hex()
login_request = Request(
    f"{base_url}/api/v2/auth/login",
    data=json.dumps({"username": "admin", "password": password_hash}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(login_request, timeout=2) as response:
    assert response.status == 200
    user = json.load(response)
assert user["is_admin"] is True
assert user["is_initial_password"] is True
PY

kill "${APP_PID}"
wait "${APP_PID}" || true
APP_PID=""
kill "${ARIA2_PID}"
wait "${ARIA2_PID}" || true
ARIA2_PID=""
printf '无 V1 独立验收通过。\n'
