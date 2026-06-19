#!/bin/zsh
# ═══════════════════════════════════════════════════════════════
#  run_dual_engines.sh
#  Chạy song song 2 engine tạo ảnh:
#    - Tiến trình 1 (Flow)    → xử lý hàng 1, 3, 5, 7 ...
#    - Tiến trình 2 (Imagen4) → xử lý hàng 2, 4, 6, 8 ...
#
#  Cách dùng:
#    ./run_dual_engines.sh
#    ./run_dual_engines.sh --sources my_sources.txt
#
#  Dừng cả 2: Ctrl+C
# ═══════════════════════════════════════════════════════════════

set -e

# ── Cấu hình ──────────────────────────────────────────────────
SOURCES="${SOURCES:-sheet_sources.txt}"
CREDENTIALS="${CREDENTIALS:-credentials.json}"
TOKEN_FILE="${TOKEN_FILE:-token.json}"
IMAGEN4_LICENSE_KEY="${IMAGEN4_LICENSE_KEY:-}"
IMAGEN4_ASPECT_RATIO="${IMAGEN4_ASPECT_RATIO:-square}"
IMAGEN4_QUALITY="${IMAGEN4_QUALITY:-2k}"
IMAGEN4_MAX_IN_FLIGHT="${IMAGEN4_MAX_IN_FLIGHT:-6}"
DRIVE_PARENT_BLACK_AUTO="${DRIVE_PARENT_BLACK_AUTO:-root}"
DRIVE_PARENT_HOA="${DRIVE_PARENT_HOA:-root}"
LOG_DIR="logs/dual_engines"

# Nhận override từ argument: --sources / --credentials / --token-file
for arg in "$@"; do
  case "$arg" in
    --sources=*)       SOURCES="${arg#*=}" ;;
    --credentials=*)   CREDENTIALS="${arg#*=}" ;;
    --token-file=*)    TOKEN_FILE="${arg#*=}" ;;
    --license-key=*)   IMAGEN4_LICENSE_KEY="${arg#*=}" ;;
    --quality=*)       IMAGEN4_QUALITY="${arg#*=}" ;;
    --max-in-flight=*) IMAGEN4_MAX_IN_FLIGHT="${arg#*=}" ;;
  esac
done

mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FLOW="$LOG_DIR/flow_${TIMESTAMP}.log"
LOG_IMAGEN4="$LOG_DIR/imagen4_${TIMESTAMP}.log"

echo "═══════════════════════════════════════════════════════════"
echo "  DUAL ENGINE RUNNER"
echo "  Sources : $SOURCES"
echo "  Log Flow    : $LOG_FLOW"
echo "  Log Imagen4 : $LOG_IMAGEN4"
echo "═══════════════════════════════════════════════════════════"

# ── Tiến trình 1: Flow — hàng offset=0 (1,3,5...) ────────────
python3 run_multi_sheet_flow.py \
  --sources "$SOURCES" \
  --credentials "$CREDENTIALS" \
  --token-file "$TOKEN_FILE" \
  --drive-parent-black-auto "$DRIVE_PARENT_BLACK_AUTO" \
  --drive-parent-hoa "$DRIVE_PARENT_HOA" \
  --generator flow \
  --row-stride 2 \
  --row-offset 0 \
  2>&1 | tee "$LOG_FLOW" | sed 's/^/[FLOW] /' &

PID_FLOW=$!
echo "[launcher] Flow PID=$PID_FLOW"

# Delay để tránh vượt quota Sheets API (60 req/phút/user)
echo "[launcher] Chờ 60s trước khi khởi động Imagen4 để tránh rate limit Sheets API..."
sleep 60

# ── Tiến trình 2: Imagen4 — hàng offset=1 (2,4,6...) ─────────
python3 run_multi_sheet_flow.py \
  --sources "$SOURCES" \
  --credentials "$CREDENTIALS" \
  --token-file "$TOKEN_FILE" \
  --drive-parent-black-auto "$DRIVE_PARENT_BLACK_AUTO" \
  --drive-parent-hoa "$DRIVE_PARENT_HOA" \
  --generator imagen4 \
  --imagen4-license-key "$IMAGEN4_LICENSE_KEY" \
  --imagen4-aspect-ratio "$IMAGEN4_ASPECT_RATIO" \
  --imagen4-quality "$IMAGEN4_QUALITY" \
  --imagen4-max-in-flight "$IMAGEN4_MAX_IN_FLIGHT" \
  --row-stride 2 \
  --row-offset 1 \
  2>&1 | tee "$LOG_IMAGEN4" | sed 's/^/[IMG4] /' &

PID_IMG4=$!
echo "[launcher] Imagen4 PID=$PID_IMG4"

echo "[launcher] Cả 2 engine đang chạy. Dừng bằng Ctrl+C."
echo "═══════════════════════════════════════════════════════════"

# Bắt Ctrl+C để kill cả 2
trap 'echo "\n[launcher] Dừng cả 2 tiến trình..."; kill $PID_FLOW $PID_IMG4 2>/dev/null; exit 0' INT TERM

# Chờ cả 2 xong
wait $PID_FLOW
EXIT_FLOW=$?
wait $PID_IMG4
EXIT_IMG4=$?

echo "═══════════════════════════════════════════════════════════"
echo "[launcher] Hoàn tất."
echo "  Flow    exit=$EXIT_FLOW | log: $LOG_FLOW"
echo "  Imagen4 exit=$EXIT_IMG4 | log: $LOG_IMAGEN4"
echo "═══════════════════════════════════════════════════════════"
