"""
Test riêng cho flow_settings_service.

Cách dùng:
─────────────────────────────────────────────────
  Test 1 — Chỉ kiểm tra đọc file config (không cần browser):
    python3 test_flow_settings.py

  Test 2 — Mở browser thật và test click panel setting:
    python3 test_flow_settings.py --profile video_1
    python3 test_flow_settings.py --profile video_2
    (tên profile phải có trong config/video_workers.json)
─────────────────────────────────────────────────
"""

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime

# ── Đường dẫn động, không hardcode máy cụ thể ──────────────────────────────
_SCRIPT_DIR   = Path(__file__).resolve().parent
_WORKERS_JSON = _SCRIPT_DIR / "config" / "video_workers.json"
_FLOW_URL     = "https://labs.google/fx/vi/tools/flow"

# ── Import service cần test ─────────────────────────────────────────────────
sys.path.insert(0, str(_SCRIPT_DIR))
from services.flow_settings_service import (
    apply_flow_generation_settings_panel,
    load_flow_ui_settings,
)

def _normalize_ui_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _model_catalog_for_mode(top_mode: str) -> tuple[list[str], str]:
    """
    Trả về danh sách model hợp lệ theo mode và model mặc định.
    """
    mode = _normalize_ui_text(top_mode)
    if mode == "video":
        models = [
            "Veo 3.1 - Fast",
            "Veo 3.1 - Quality",
            "Veo 3.1 - Lite",
            "Veo 3.1 - Fast [Lower Priority]",
            "Veo 3.1 - Lite [Lower Priority]",
        ]
        return models, "Veo 3.1 - Fast"
    models = [
        "Nano Banana Pro",
        "Nano Banana 2",
        "Imagen 4",
    ]
    return models, "Nano Banana Pro"


def _ratio_catalog_for_mode(top_mode: str) -> tuple[list[str], str]:
    """
    Trả về danh sách ratio hợp lệ theo mode và ratio mặc định.
    """
    mode = _normalize_ui_text(top_mode)
    if mode == "video":
        return ["9:16", "16:9"], "16:9"
    return ["16:9", "4:3", "1:1", "3:4", "9:16"], "1:1"


def _is_ratio_valid_for_mode(aspect_ratio: str, top_mode: str) -> bool:
    ratio = str(aspect_ratio or "").replace(" ", "")
    allowed, _ = _ratio_catalog_for_mode(top_mode)
    allowed_norm = [str(x).replace(" ", "") for x in allowed]
    return ratio in allowed_norm


def _is_model_valid_for_mode(model_name: str, top_mode: str) -> bool:
    """
    Kiểm tra model hiện tại có thuộc mode đã chọn hay không.
    Chấp nhận alias Nano Banana 2 <-> Nano Banana Pro.
    """
    model = _normalize_ui_text(model_name)
    allowed, _ = _model_catalog_for_mode(top_mode)
    allowed_norm = [_normalize_ui_text(x) for x in allowed]
    if any(a in model or model in a for a in allowed_norm):
        return True
    if "nano banana 2" in model and any("nano banana pro" in a for a in allowed_norm):
        return True
    if "nano banana pro" in model and any("nano banana 2" in a for a in allowed_norm):
        return True
    return False


# ════════════════════════════════════════════════════════════════════════════
#  TEST 1 — Đọc file config (không cần browser)
# ════════════════════════════════════════════════════════════════════════════

def test_config_only() -> bool:
    """
    Kiểm tra load_flow_ui_settings() đọc đúng file config/flow_ui_settings.txt.

    Không cần mở browser, chạy ngay được.
    Trả về True nếu pass, False nếu có vấn đề.
    """
    print()
    print("═" * 60)
    print("  TEST 1 — Đọc file config/flow_ui_settings.txt")
    print("═" * 60)

    cfg = load_flow_ui_settings()

    # Danh sách các key bắt buộc phải có
    required_keys = [
        "auto_apply",
        "top_mode",
        "secondary_mode",
        "aspect_ratio",
        "multiplier",
        "model_name",
        "allow_model_alias_fallback",
    ]

    all_ok = True
    for key in required_keys:
        val = cfg.get(key)
        if val is None:
            print(f"  ❌ Thiếu key: '{key}'")
            all_ok = False
        else:
            print(f"  ✅ {key:<20} = {repr(val)}")

    # Kiểm tra thêm kiểu dữ liệu
    print()
    if not isinstance(cfg.get("auto_apply"), bool):
        print("  ⚠️  auto_apply phải là bool (True/False)")
        all_ok = False
    else:
        print(f"  ✅ auto_apply kiểu bool   = {cfg['auto_apply']}")

    valid_modes     = {"video", "image"}
    valid_multis    = {"x1", "x2", "x3", "x4"}

    if cfg.get("top_mode", "").lower() not in valid_modes:
        print(f"  ⚠️  top_mode '{cfg.get('top_mode')}' không hợp lệ. Hợp lệ: {valid_modes}")
        all_ok = False
    allowed_ratios, default_ratio = _ratio_catalog_for_mode(cfg.get("top_mode", "image"))
    if not _is_ratio_valid_for_mode(cfg.get("aspect_ratio"), cfg.get("top_mode", "image")):
        print(
            "  ⚠️  aspect_ratio không khớp top_mode: "
            f"'{cfg.get('aspect_ratio')}' với mode '{cfg.get('top_mode')}'."
        )
        print(f"      Ratio hợp lệ cho mode này: {allowed_ratios}")
        print(f"      Gợi ý ratio mặc định: {default_ratio}")
        all_ok = False
    if cfg.get("multiplier", "").lower() not in valid_multis:
        print(f"  ⚠️  multiplier '{cfg.get('multiplier')}' không hợp lệ. Hợp lệ: {valid_multis}")
        all_ok = False
    allowed_models, default_model = _model_catalog_for_mode(cfg.get("top_mode", "image"))
    if not _is_model_valid_for_mode(cfg.get("model_name", ""), cfg.get("top_mode", "image")):
        print(
            "  ⚠️  model_name không khớp top_mode: "
            f"'{cfg.get('model_name')}' với mode '{cfg.get('top_mode')}'."
        )
        print(f"      Model hợp lệ cho mode này: {allowed_models}")
        print(f"      Gợi ý model mặc định: {default_model}")
        all_ok = False

    print()
    if all_ok:
        print("  ✅ TEST 1 PASS — Config đọc đúng và hợp lệ")
    else:
        print("  ❌ TEST 1 FAIL — Có giá trị không hợp lệ, kiểm tra file config")
    print()
    return all_ok


# ════════════════════════════════════════════════════════════════════════════
#  TEST 2 — Click thật trên browser (cần profile)
# ════════════════════════════════════════════════════════════════════════════

def _load_worker_config(worker_id: str) -> dict | None:
    """
    Đọc config của 1 worker từ config/video_workers.json theo worker_id.
    Trả về dict worker nếu tìm thấy, None nếu không.
    """
    if not _WORKERS_JSON.exists():
        print(f"  ❌ Không tìm thấy file: {_WORKERS_JSON}")
        return None
    data = json.loads(_WORKERS_JSON.read_text(encoding="utf-8"))
    for w in data.get("video_workers", []):
        if w.get("worker_id") == worker_id:
            return w
    return None


async def _open_new_project(page, timeout_sec: int = 45) -> bool:
    """
    Tự động click nút "Dự án mới" trên trang chủ Google Flow.
    Copy logic từ open_google_flow_new_project() trong dreamina.py.

    Trả về True khi đã vào editor có ô nhập prompt.
    """
    import re

    FLOW_HOME = "https://labs.google/fx/vi/tools/flow"

    # Điều hướng về trang chủ Flow trước
    print("  🌐 Điều hướng về trang chủ Google Flow...")
    try:
        await page.goto(FLOW_HOME, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2.0)
    except Exception as e:
        print(f"  ❌ Lỗi mở trang chủ: {e}")
        return False

    # Nếu đang ở project URL → về trang chủ trước
    if "/project/" in (page.url or ""):
        try:
            await page.goto(FLOW_HOME, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)
        except Exception:
            pass

    start = asyncio.get_event_loop().time()

    # Các pattern text của nút "Dự án mới" (tiếng Việt / English)
    new_project_patterns = [
        re.compile(r"dự án mới", re.IGNORECASE),
        re.compile(r"new project", re.IGNORECASE),
        re.compile(r"create project", re.IGNORECASE),
        re.compile(r"tạo dự án", re.IGNORECASE),
    ]

    # Các selector để nhận biết editor đã sẵn sàng (có ô nhập prompt)
    editor_selectors = [
        "div[contenteditable='true']",
        "div[role='textbox']",
        "textarea",
    ]

    print("  🔍 Đang tìm nút 'Dự án mới'...")

    while (asyncio.get_event_loop().time() - start) < timeout_sec:
        # Thử click nút theo text pattern
        for patt in new_project_patterns:
            try:
                btn = page.get_by_text(patt).first
                if await btn.count() > 0 and await btn.is_visible():
                    print(f"  ✅ Tìm thấy nút '{patt.pattern}' → đang click...")
                    await btn.click()
                    await asyncio.sleep(2.5)
                    # Kiểm tra đã vào project URL chưa
                    if "/project/" in (page.url or ""):
                        # Chờ editor có ô nhập prompt
                        for sel in editor_selectors:
                            try:
                                el = page.locator(sel).first
                                if await el.count() > 0 and await el.is_visible(timeout=5000):
                                    print(f"  ✅ Editor sẵn sàng! URL: {page.url}")
                                    return True
                            except Exception:
                                pass
            except Exception:
                pass

        # Fallback: tìm button có icon add_2 kèm text tạo project
        try:
            btn = page.locator("button").filter(
                has_text=re.compile(
                    r"(add_2).*(dự án mới|new project|create project|tạo dự án)",
                    re.IGNORECASE,
                )
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                print("  ✅ Tìm thấy nút tạo project (fallback) → đang click...")
                await btn.click()
                await asyncio.sleep(2.5)
                if "/project/" in (page.url or ""):
                    for sel in editor_selectors:
                        try:
                            el = page.locator(sel).first
                            if await el.count() > 0 and await el.is_visible(timeout=5000):
                                print(f"  ✅ Editor sẵn sàng! URL: {page.url}")
                                return True
                        except Exception:
                            pass
        except Exception:
            pass

        await asyncio.sleep(1.0)

    return False


async def test_browser_click(worker_id: str) -> bool:
    """
    Mở Chrome với profile của worker_id, điều hướng đến Google Flow,
    rồi gọi apply_flow_generation_settings_panel() theo config hiện tại.

    Báo cáo từng bước: OK / FAIL / lỗi chi tiết.
    """
    from playwright.async_api import async_playwright

    print()
    print("═" * 60)
    print(f"  TEST 2 — Click panel setting trên browser ({worker_id})")
    print("═" * 60)

    # Tạo thư mục debug riêng cho lần test này để gom đủ artefact.
    # Mục tiêu: chỉ cần chạy 1 lần là có đủ dữ liệu phân tích selector.
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_dir = _SCRIPT_DIR / "debug_sessions" / "flow_settings" / f"{worker_id}_{run_id}"
    debug_dir.mkdir(parents=True, exist_ok=True)

    # Đọc config worker
    worker = _load_worker_config(worker_id)
    if not worker:
        print(f"  ❌ Không tìm thấy worker '{worker_id}' trong {_WORKERS_JSON.name}")
        print(f"     Các worker hợp lệ: video_1 → video_10")
        return False

    # Đường dẫn profile (động, không hardcode)
    profile_dir = _SCRIPT_DIR / worker["profile_dir"]
    proxy_url   = worker.get("proxy")  # ví dụ: "socks5://127.0.0.1:11001"

    print(f"  Profile : {profile_dir}")
    print(f"  Proxy   : {proxy_url or 'không dùng'}")
    print(f"  Debug   : {debug_dir}")

    if not profile_dir.exists():
        print(f"  ❌ Profile chưa tồn tại: {profile_dir}")
        print(f"     Hãy chạy login trước: python3 login_video_profile_multi.py")
        return False

    # Đọc setting từ file config
    cfg = load_flow_ui_settings()
    print()
    print("  Cài đặt sẽ áp dụng (từ config/flow_ui_settings.txt):")
    print(f"    top_mode       = {cfg['top_mode']}")
    print(f"    secondary_mode = {cfg['secondary_mode'] or '(bỏ qua)'}")
    print(f"    aspect_ratio   = {cfg['aspect_ratio']}")
    print(f"    multiplier     = {cfg['multiplier']}")
    print(f"    model_name     = {cfg['model_name']}")
    print(f"    alias_fallback = {cfg.get('allow_model_alias_fallback', False)}")
    print()

    # Log collector để in ra terminal
    log_lines: list[str] = []

    def log_cb(msg: str, level: str = "DBG") -> None:
        """Callback nhận log từ service và in ra terminal."""
        icon = {
            "OK":   "✅",
            "ERR":  "❌",
            "WARN": "⚠️ ",
            "STEP": "🔵",
            "DBG":  "   ",
        }.get(level, "   ")
        line = f"  {icon} [{level}] {msg}"
        log_lines.append(line)
        print(line)

    # Khởi động browser
    print("  🌐 Đang mở Chrome...")
    async with async_playwright() as p:
        # Build proxy config nếu có
        proxy_cfg = {"server": proxy_url} if proxy_url else None

        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,          # Hiện browser để bạn nhìn thấy
            channel="chrome",
            proxy=proxy_cfg,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1440, "height": 900},
        )

        page = await browser.new_page()

        # ── Tự động mở "Dự án mới" giống dreamina.py ──────────────────────
        # Panel setting chỉ xuất hiện khi đã vào editor của 1 project cụ thể.
        print()
        print("  🔧 Đang tự động click 'Dự án mới' để vào Editor...")
        editor_ready = await _open_new_project(page, timeout_sec=45)

        if not editor_ready:
            print("  ❌ Không vào được Editor sau 45s.")
            print("     Kiểm tra: đã đăng nhập Google chưa? Mạng có ổn không?")
            await browser.close()
            return False

        print("  🟢 Đã vào Editor — bắt đầu apply setting...")
        print()

        # Gọi hàm apply setting
        result = await apply_flow_generation_settings_panel(
            page,
            top_mode       = cfg["top_mode"],
            secondary_mode = cfg["secondary_mode"],
            aspect_ratio   = cfg["aspect_ratio"],
            multiplier     = cfg["multiplier"],
            model_name     = cfg["model_name"],
            allow_model_alias_fallback = bool(cfg.get("allow_model_alias_fallback", False)),
            log_cb         = log_cb,
            debug_dir      = str(debug_dir),
        )

        # ── Hiện kết quả sơ bộ, chờ user check trực quan ──────────────────
        overall_ok = result.get("ok", False)
        print()
        if overall_ok:
            print("  ✅ Apply xong! Hãy kiểm tra panel trên Chrome.")
        else:
            print("  ⚠️  Apply có bước chưa thành công. Kiểm tra Chrome để xác nhận.")

        print()
        print("  " + "─" * 56)
        print("  👀 Hãy nhìn vào Chrome và kiểm tra setting đã đúng chưa.")
        print("     Sau khi kiểm tra xong, nhấn [Enter] để xem báo cáo")
        print("     chi tiết và đóng browser.")
        print("  " + "─" * 56)
        print()

        await asyncio.get_event_loop().run_in_executor(
            None,
            input,
            "  👉 Nhấn [Enter] để xem báo cáo và đóng browser... ",
        )

        # ── BÁO CÁO CHI TIẾT sau khi user đã check ────────────────────────
        print()
        print("  ═" * 30)
        print("  BÁO CÁO KẾT QUẢ TỪNG BƯỚC")
        print("  ═" * 30)

        applied = result.get("applied", {})
        for key, ok in applied.items():
            icon = "✅" if ok else "❌"
            print(f"  {icon}  {key:<20} → {'OK' if ok else 'FAIL'}")

        # In lỗi chi tiết nếu có
        errors = result.get("errors", [])
        if errors:
            print()
            print("  ─── LỖI CHI TIẾT ──────────────────────────────────")
            for err in errors:
                print(f"  ❌ {err}")

        # Các bước đã click thành công
        steps = result.get("steps", [])
        if steps:
            print()
            print(f"  ─── ĐÃ CLICK THÀNH CÔNG ({len(steps)} bước) ─────────────")
            for s in steps:
                print(f"  ✅ {s}")

        print()
        print(f"  📁 Debug artifacts: {debug_dir}")
        print("     - *_flow_settings_result.json: full result + traces")
        print("     - *_global_before/after.png: toàn màn hình trước/sau")
        print("     - *_<step>_before/after.png: ảnh từng bước")
        print()

        if overall_ok:
            print("  ✅ TEST 2 PASS — Panel setting đã được áp dụng thành công!")
        else:
            print("  ❌ TEST 2 FAIL — Một số bước không click được.")
        print()

        await browser.close()
        print("  🔒 Đã đóng browser.")

    return overall_ok


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    args = sys.argv[1:]

    # Lấy tên profile nếu truyền --profile video_X
    profile = None
    if "--profile" in args:
        idx = args.index("--profile")
        if idx + 1 < len(args):
            profile = args[idx + 1]

    # Luôn chạy TEST 1 trước
    config_ok = test_config_only()

    # Nếu có truyền profile → chạy TEST 2
    if profile:
        if not config_ok:
            print("  ⚠️  Config có vấn đề nhưng vẫn tiếp tục test browser...")
        await test_browser_click(profile)
    else:
        print("  💡 Để test click trên browser thật, chạy:")
        print("     python3 test_flow_settings.py --profile video_1")
        print()


if __name__ == "__main__":
    asyncio.run(main())
