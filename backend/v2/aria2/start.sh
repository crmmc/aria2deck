#!/usr/bin/env bash
set -euo pipefail

if ! command -v aria2c >/dev/null 2>&1; then
    printf 'aria2c 未安装，请先安装 aria2。\n' >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." && pwd)"
RUNTIME_DIR="${ARIA2DECK_ARIA2_RUNTIME_DIR:-${SCRIPT_DIR}/runtime}"
DOWNLOAD_DIR="${ARIA2DECK_DOWNLOAD_DIR:-${REPO_ROOT}/data/downloads}"
RPC_PORT="${ARIA2DECK_ARIA2_RPC_PORT:-6800}"
RPC_SECRET="${ARIA2DECK_ARIA2_RPC_SECRET:-v2-local-development-only}"
SESSION_FILE="${RUNTIME_DIR}/aria2.session"

mkdir -p "${DOWNLOAD_DIR}" "${RUNTIME_DIR}"
touch "${SESSION_FILE}"

printf 'aria2 RPC: http://127.0.0.1:%s/jsonrpc\n' "${RPC_PORT}"
printf '下载目录: %s\n' "${DOWNLOAD_DIR}"

exec aria2c \
    --enable-rpc=true \
    --rpc-listen-all=false \
    --rpc-listen-port="${RPC_PORT}" \
    --rpc-secret="${RPC_SECRET}" \
    --dir="${DOWNLOAD_DIR}" \
    --input-file="${SESSION_FILE}" \
    --save-session="${SESSION_FILE}" \
    --save-session-interval=1 \
    --dht-file-path="${RUNTIME_DIR}/DHT.dat" \
    --dht-file-path6="${RUNTIME_DIR}/DHT6.dat" \
    --file-allocation=none \
    --log="${RUNTIME_DIR}/aria2.log" \
    --log-level=warn \
    --console-log-level=info \
    --summary-interval=5
