#!/usr/bin/env bash

set -u

TASK_ID="${1:-}"
if [[ -n "$TASK_ID" ]]; then
  OUTPUT_FILE="${2:-./task-${TASK_ID}-watch.log}"
  BASE_DIR="${3:-/Downloads/aria2deck}"
else
  OUTPUT_FILE="${2:-./task-watch.log}"
  BASE_DIR="${1:-/Downloads/aria2deck}"
fi

DOWNLOADING_ROOT="${BASE_DIR}/downloading"
STORE_DIR="${BASE_DIR}/store"

echo "[watch] task_id=${TASK_ID:-ALL}" | tee -a "$OUTPUT_FILE"
echo "[watch] base_dir=${BASE_DIR}" | tee -a "$OUTPUT_FILE"
echo "[watch] downloading_root=${DOWNLOADING_ROOT}" | tee -a "$OUTPUT_FILE"
echo "[watch] store_dir=${STORE_DIR}" | tee -a "$OUTPUT_FILE"
echo "[watch] interval=1s" | tee -a "$OUTPUT_FILE"
echo "[watch] start_time=$(date '+%F %T %z')" | tee -a "$OUTPUT_FILE"

while true; do
  {
    echo ""
    echo "===== $(date '+%F %T.%3N %z') ====="

    echo "[dir] ${BASE_DIR}"
    ls -lah "${BASE_DIR}" 2>&1

    echo "[dir] ${DOWNLOADING_ROOT}"
    ls -lah "${DOWNLOADING_ROOT}" 2>&1

    if [[ -n "$TASK_ID" ]]; then
      echo "[dir] ${DOWNLOADING_ROOT}/${TASK_ID}"
      ls -lah "${DOWNLOADING_ROOT}/${TASK_ID}" 2>&1
    else
      if [[ -d "$DOWNLOADING_ROOT" ]]; then
        for dir in "$DOWNLOADING_ROOT"/*; do
          [[ -d "$dir" ]] || continue
          echo "[task-dir] ${dir}"
          ls -lah "$dir" 2>&1
        done
      fi
    fi

    echo "[dir] ${STORE_DIR}"
    ls -lah "${STORE_DIR}" 2>&1
  } >> "$OUTPUT_FILE"

  sleep 1
done
