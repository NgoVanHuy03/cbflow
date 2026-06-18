#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"
VENV_ACTIVATE="${VENV_DIR}/bin/activate"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "[auto] Khong tim thay ${VENV_DIR}. Dang tao moi truong ao..."
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_ACTIVATE"

mkdir -p logs/auto
TS=$(date +%Y%m%d_%H%M%S)
LOG_FILE="logs/auto/run_${TS}.log"

echo "[auto] Log file: ${LOG_FILE}"
echo "[auto] Python: $VENV_PYTHON"

"$VENV_PYTHON" run_multi_sheet_flow.py "$@" 2>&1 | tee "$LOG_FILE"
