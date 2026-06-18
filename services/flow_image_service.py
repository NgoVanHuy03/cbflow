"""
Service chờ render và tải ảnh từ Google Flow.

Mục tiêu:
- Tách riêng logic wait + download ảnh khỏi dreamina.py.
- Dễ gọi độc lập sau khi send_flow_prompt() xong.
- Hỗ trợ cả 2 phương thức tải: fetch API và canvas fallback.

Luồng gọi thông thường:
    baseline = await capture_flow_baseline_srcs(page)
    await send_flow_prompt(page)  # từ flow_prompt_service
    new_srcs = await wait_for_flow_images(page, baseline, expected=3, timeout=120)
    saved = await download_flow_images(page, new_srcs, output_dir, prefix="scene_01")
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Callable


# ── Đường dẫn động ─────────────────────────────────────────────────────────
_SERVICE_DIR = Path(__file__).resolve().parent


def _log(msg: str, level: str = "DBG") -> None:
    """Log đơn giản ra stdout, không cần import dreamina.py."""
    icon = {
        "OK":   "✅",
        "WARN": "⚠️ ",
        "ERR":  "❌",
        "DBG":  "   ",
    }.get(level, "   ")
    print(f"  {icon} [flow_image] {msg}", flush=True)


def _safe_filename(text: str, max_len: int = 50) -> str:
    """
    Tạo tên file an toàn từ text prompt.
    Loại bỏ ký tự đặc biệt, giữ chữ/số/dấu gạch dưới.
    """
    raw_text = str(text or "")
    name = re.sub(r"[^\w\sàáảãạăắặẳẵầấẩẫậêếệểễôốộổỗơớợởỡùúủũụưứựửữđ]", "", raw_text, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    # Luôn gắn hậu tố hash để tránh trùng tên khi prefix dài bị cắt.
    # Ví dụ: job_id dài có thể làm mất phần "_001", "_002" nếu chỉ cắt chuỗi thuần.
    digest = hashlib.sha1(raw_text.encode("utf-8")).hexdigest()[:10]
    if not name:
        return f"prompt_{digest}"
    # Dành chỗ cho "_{digest}" để mỗi prefix luôn tạo tên file khác nhau.
    keep = max(1, int(max_len) - (len(digest) + 1))
    return f"{name[:keep]}_{digest}"


# ════════════════════════════════════════════════════════════════════════════
#  LẤY SRC ẢNH HIỆN TẠI
# ════════════════════════════════════════════════════════════════════════════

async def get_flow_image_srcs(page) -> set:
    """
    Lấy src của tất cả ảnh đã load xong trên trang.

    Bắt cả 2 dạng:
    - <img> tag đã load xong (naturalWidth > 100)
    - CSS background-image (Google Flow đôi khi dùng)
    """
    srcs: set = set()
    try:
        result = await page.evaluate(
            """
            () => {
                const srcs = new Set();

                // 1. Thẻ <img> đã load xong
                document.querySelectorAll('img').forEach(img => {
                    const src = img.src || img.currentSrc || '';
                    if (src && img.complete && img.naturalWidth > 100 && img.naturalHeight > 100) {
                        srcs.add(src);
                    }
                });

                // 2. CSS background-image (Google Flow đôi khi dùng)
                document.querySelectorAll('[style*="background-image"]').forEach(el => {
                    const style = el.style.backgroundImage || '';
                    const match = style.match(/url\\(["']?([^"')]+)["']?\\)/);
                    if (match && match[1] && !match[1].startsWith('data:image/svg')) {
                        srcs.add(match[1]);
                    }
                });

                return Array.from(srcs);
            }
            """
        )
        srcs = set(result or [])
    except Exception:
        pass
    return srcs


async def get_flow_image_entries(page) -> list:
    """
    Lấy danh sách ảnh theo thứ tự DOM để giữ đúng thứ tự hiển thị.
    Mỗi phần tử gồm: src, top, left, width, height.
    Dùng để map prompt -> ảnh theo vị trí.
    """
    try:
        entries = await page.evaluate(
            """
            () => {
                const out = [];
                const imgs = Array.from(document.querySelectorAll('img'));
                for (const img of imgs) {
                    const src = img.currentSrc || img.src || '';
                    if (!src) continue;
                    if (!img.complete) continue;
                    if ((img.naturalWidth || 0) <= 100 || (img.naturalHeight || 0) <= 100) continue;

                    const r = img.getBoundingClientRect();
                    out.push({
                        src,
                        top:    Math.round(r.top),
                        left:   Math.round(r.left),
                        width:  img.naturalWidth  || Math.round(r.width),
                        height: img.naturalHeight || Math.round(r.height),
                    });
                }
                return out;
            }
            """
        )
        return entries or []
    except Exception:
        return []


def _filter_image_srcs(srcs: set) -> set:
    """
    Lọc bỏ icon / logo / spinner / placeholder.
    Chỉ giữ lại ảnh generate thực sự.
    """
    SKIP_KEYWORDS = ["logo", "icon", "avatar", "favicon", "spinner", "loading", "placeholder"]
    return {
        s for s in srcs
        if not any(kw in s.lower() for kw in SKIP_KEYWORDS)
    }


# ════════════════════════════════════════════════════════════════════════════
#  KIỂM TRA ĐANG RENDER
# ════════════════════════════════════════════════════════════════════════════

async def is_flow_generating(page) -> bool:
    """
    Kiểm tra Google Flow có đang render ảnh không.
    Tìm spinner/skeleton visible trên trang.
    """
    try:
        generating = await page.evaluate(
            """
            () => {
                const specific = [
                    '[class*="skeleton"]',
                    '[class*="shimmer"]',
                    '[class*="spinner"]',
                    'svg[class*="spin"]',
                    '[class*="generating"]',
                    '[class*="pending"]',
                ];
                for (const sel of specific) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        if (el.offsetWidth > 0 && el.offsetHeight > 0) return true;
                    }
                }
                return false;
            }
            """
        )
        return bool(generating)
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
#  SCROLL ĐỂ TRIGGER LAZY-LOAD
# ════════════════════════════════════════════════════════════════════════════

async def scroll_flow_page_to_load_all(page) -> None:
    """
    Scroll toàn trang để trigger lazy-load ảnh cũ.
    Dùng trước khi chụp baseline để tránh bỏ sót ảnh ngoài viewport.
    """
    try:
        await page.evaluate(
            """
            async () => {
                await new Promise(resolve => {
                    let last = 0;
                    const step = () => {
                        window.scrollBy(0, 600);
                        const cur = document.documentElement.scrollTop;
                        if (cur === last) { window.scrollTo(0, 0); resolve(); }
                        else { last = cur; setTimeout(step, 120); }
                    };
                    step();
                });
            }
            """
        )
        await asyncio.sleep(1)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
#  CHỤP BASELINE ẢNH CŨ
# ════════════════════════════════════════════════════════════════════════════

async def capture_flow_baseline_srcs(page, max_rounds: int = 4) -> set:
    """
    Chụp baseline ảnh cũ ổn định trước khi gửi prompt mới.

    Cách làm:
    1. Scroll để trigger lazy-load
    2. Lấy tập src ảnh hiện có
    3. Lặp đến khi số lượng không tăng thêm (ổn định) → baseline đáng tin

    Trả về set src ảnh hiện có TRƯỚC khi generate.
    """
    baseline: set = set()
    prev_count = -1

    for round_idx in range(1, max_rounds + 1):
        await scroll_flow_page_to_load_all(page)
        await asyncio.sleep(1)
        current = await get_flow_image_srcs(page)

        # Union để tích lũy đầy đủ ảnh cũ đã thấy
        baseline |= current
        cur_count = len(baseline)
        _log(f"Baseline round {round_idx}/{max_rounds}: {cur_count} ảnh")

        # Ổn định 1 vòng liên tiếp → dừng sớm
        if cur_count == prev_count:
            break
        prev_count = cur_count

    return baseline


# ════════════════════════════════════════════════════════════════════════════
#  CHỜ ẢNH MỚI RENDER XONG
# ════════════════════════════════════════════════════════════════════════════

async def wait_for_flow_images(
    page,
    before_srcs: set,
    expected: int = 1,
    timeout: int = 120,
    log_cb: Callable[[str, str], None] | None = None,
) -> list:
    """
    Chờ đến khi ảnh mới xuất hiện và ổn định sau khi gửi prompt.

    Điều kiện xong:
    - Số ảnh mới không tăng thêm trong 12s liên tiếp (stable_count >= 4 × 3s)
    - Không còn spinner/generating trên trang (hoặc đã stable >= 18s)

    Args:
        page: Playwright page object
        before_srcs: Set src ảnh TRƯỚC khi generate (baseline)
        expected: Số ảnh kỳ vọng (dùng để log tiến độ)
        timeout: Giới hạn thời gian chờ (giây)
        log_cb: Callback log tuỳ chọn, nhận (msg, level)

    Returns:
        Danh sách src ảnh mới xuất hiện sau generate.
    """
    def _cb(msg: str, level: str = "DBG") -> None:
        if callable(log_cb):
            log_cb(msg, level)
        else:
            _log(msg, level)

    _cb(f"Chờ render {expected} ảnh...", "DBG")
    start        = time.time()
    stable_count = 0
    last_count   = 0

    # Chờ generate bắt đầu (tối đa 15s)
    for _ in range(8):
        if await is_flow_generating(page):
            break
        await asyncio.sleep(2)

    while time.time() - start < timeout:
        await asyncio.sleep(3)

        current      = await get_flow_image_srcs(page)
        new_srcs     = _filter_image_srcs(current - before_srcs)
        still_render = await is_flow_generating(page)

        if len(new_srcs) > last_count:
            last_count   = len(new_srcs)
            stable_count = 0
            _cb(f"Đang render... ({len(new_srcs)}/{expected} ảnh)", "DBG")
        else:
            stable_count += 1

        # Ổn định 12s → xong
        if new_srcs and stable_count >= 4:
            if not still_render or stable_count >= 6:
                _cb(f"Xong! {len(new_srcs)} ảnh mới", "OK")
                return list(new_srcs)

        # Log tiến độ mỗi 20s
        elapsed = int(time.time() - start)
        if elapsed > 0 and elapsed % 20 == 0:
            status = "render" if still_render else "ổn định"
            _cb(f"[{status}] {elapsed}s — {len(new_srcs)} ảnh mới", "DBG")

    _cb("Timeout — lấy toàn bộ ảnh hiện có", "WARN")
    current = await get_flow_image_srcs(page)
    return list(_filter_image_srcs(current - before_srcs))


# ════════════════════════════════════════════════════════════════════════════
#  TẢI ẢNH
# ════════════════════════════════════════════════════════════════════════════

async def download_flow_images(
    page,
    new_srcs: list,
    output_dir: str,
    prefix: str = "img",
    max_download: int | None = None,
    log_cb: Callable[[str, str], None] | None = None,
) -> int:
    """
    Tải ảnh mới generate về thư mục output.

    Phương thức:
    1. Fetch API qua JS (ưu tiên, giữ nguyên chất lượng)
    2. Canvas fallback (nếu fetch bị chặn CORS)

    Args:
        page: Playwright page object
        new_srcs: Danh sách URL ảnh cần tải (từ wait_for_flow_images)
        output_dir: Thư mục lưu ảnh (sẽ tạo nếu chưa có)
        prefix: Tiền tố tên file (vd: "scene_01" → "scene_01_img1.png")
        max_download: Giới hạn số ảnh cần tải.
            - None / <=0: tải toàn bộ ảnh trong new_srcs.
            - >0: chỉ tải tối đa N ảnh đầu tiên.
        log_cb: Callback log tuỳ chọn

    Returns:
        Số ảnh đã tải thành công.
    """
    def _cb(msg: str, level: str = "DBG") -> None:
        if callable(log_cb):
            log_cb(msg, level)
        else:
            _log(msg, level)

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    saved = 0
    safe_prefix = _safe_filename(prefix)

    # Cho phép giới hạn số ảnh tải để tối ưu băng thông/dung lượng.
    # Dùng nhiều nhất ở stage reference: chỉ cần 1 ảnh đại diện.
    srcs_to_download = list(new_srcs or [])
    if max_download is not None and int(max_download) > 0:
        srcs_to_download = srcs_to_download[: int(max_download)]
        _cb(
            f"Giới hạn tải ảnh: {len(srcs_to_download)}/{len(new_srcs or [])} (max_download={int(max_download)})",
            "DBG",
        )

    for idx, src in enumerate(srcs_to_download):
        filepath = os.path.join(output_dir, f"{safe_prefix}_img{idx + 1}.png")
        try:
            # ── Phương thức 1: Request từ BrowserContext (ổn định hơn evaluate/fetch) ──
            if src.startswith("http"):
                request_errors: list[str] = []
                for attempt in range(1, 4):
                    try:
                        resp = await page.context.request.get(src, timeout=45000)
                        status = int(resp.status or 0)
                        ct = str(resp.headers.get("content-type", "") or "").lower()
                        raw = await resp.body()
                        size_kb = max(0, len(raw) // 1024)
                        looks_like_image = ct.startswith("image/")
                        if not looks_like_image and raw[:8] in (
                            b"\x89PNG\r\n\x1a\n",
                            b"\xff\xd8\xff\xe0",
                            b"\xff\xd8\xff\xe1",
                            b"\xff\xd8\xff\xdb",
                            b"RIFF",
                        ):
                            looks_like_image = True

                        if 200 <= status < 300 and looks_like_image and len(raw) > 1024:
                            with open(filepath, "wb") as f:
                                f.write(raw)
                            _cb(
                                f"Đã lưu: {os.path.basename(filepath)} ({size_kb}KB) "
                                f"[request, attempt={attempt}, status={status}]",
                                "OK",
                            )
                            saved += 1
                            break

                        request_errors.append(
                            f"attempt={attempt}, status={status}, ct='{ct or 'n/a'}', size={size_kb}KB"
                        )
                    except Exception as exc:
                        request_errors.append(f"attempt={attempt}, exc={exc}")

                    await asyncio.sleep(0.6 * attempt)

                if saved > idx:
                    # Ảnh hiện tại đã save thành công.
                    continue

                if request_errors:
                    _cb(
                        f"request.get chưa lấy được ảnh cho src#{idx + 1}: "
                        + " | ".join(request_errors),
                        "WARN",
                    )

            # ── Phương thức 2: Fetch API trong page ──
            if src.startswith("http"):
                data = await page.evaluate(
                    """
                    async (src) => {
                        try {
                            const r = await fetch(src);
                            const b = await r.blob();
                            return await new Promise(res => {
                                const reader = new FileReader();
                                reader.onloadend = () => res(reader.result);
                                reader.readAsDataURL(b);
                            });
                        } catch { return null; }
                    }
                    """,
                    src,
                )
                if data and data.startswith("data:"):
                    raw = base64.b64decode(data.split(",", 1)[1])
                    with open(filepath, "wb") as f:
                        f.write(raw)
                    size_kb = os.path.getsize(filepath) // 1024
                    if size_kb > 5:
                        _cb(f"Đã lưu: {os.path.basename(filepath)} ({size_kb}KB) [evaluate/fetch]", "OK")
                        saved += 1
                        continue

            # ── Phương thức 2: Canvas fallback ──
            data = await page.evaluate(
                """
                (src) => {
                    const img = document.querySelector(`img[src="${src}"]`);
                    if (!img) return null;
                    const c = document.createElement('canvas');
                    c.width  = img.naturalWidth  || img.width;
                    c.height = img.naturalHeight || img.height;
                    c.getContext('2d').drawImage(img, 0, 0);
                    return c.toDataURL('image/png');
                }
                """,
                src,
            )
            if data and data.startswith("data:"):
                raw = base64.b64decode(data.split(",", 1)[1])
                with open(filepath, "wb") as f:
                    f.write(raw)
                size_kb = os.path.getsize(filepath) // 1024
                if size_kb > 5:
                    _cb(f"Đã lưu (canvas): {os.path.basename(filepath)} ({size_kb}KB)", "OK")
                    saved += 1

        except Exception as exc:
            _cb(f"Lỗi tải ảnh #{idx + 1}: {exc}", "ERR")

    _cb(
        f"Tổng: {saved}/{len(srcs_to_download)} ảnh đã tải",
        "OK" if saved == len(srcs_to_download) else "WARN",
    )
    return saved


# ════════════════════════════════════════════════════════════════════════════
#  TIỆN ÍCH: CHẠY CẢ FLOW (baseline → chờ → tải)
# ════════════════════════════════════════════════════════════════════════════

async def run_flow_image_capture(
    page,
    output_dir: str,
    prefix: str = "img",
    expected: int = 1,
    timeout: int = 120,
    max_download: int | None = None,
    log_cb: Callable[[str, str], None] | None = None,
) -> dict:
    """
    Chạy toàn bộ pipeline sau khi đã gửi prompt:
    1. Chụp baseline ảnh trước generate
    2. Gửi signal "đã gửi prompt rồi, bắt đầu chờ"
    3. Chờ ảnh render xong
    4. Tải ảnh về

    ⚠️ Hàm này phải được gọi SAU send_flow_prompt().
       Nếu muốn chụp baseline TRƯỚC khi gửi prompt, gọi
       capture_flow_baseline_srcs() riêng trước.

    Returns:
        dict gồm:
        - ok: bool
        - saved: số ảnh đã tải
        - new_srcs: list URL ảnh mới
        - baseline_count: số ảnh baseline
        - download_limit: giới hạn tải đã dùng (nếu có)
    """
    def _cb(msg: str, level: str = "DBG") -> None:
        if callable(log_cb):
            log_cb(msg, level)
        else:
            _log(msg, level)

    # Chụp baseline (gọi ngay trước generate để tránh bỏ sót ảnh)
    _cb("Đang chụp baseline ảnh cũ...", "DBG")
    baseline = await capture_flow_baseline_srcs(page)
    _cb(f"Baseline: {len(baseline)} ảnh", "DBG")

    # Chờ ảnh mới
    new_srcs = await wait_for_flow_images(
        page,
        before_srcs=baseline,
        expected=expected,
        timeout=timeout,
        log_cb=log_cb,
    )

    if not new_srcs:
        _cb("Không tìm thấy ảnh mới sau generate", "WARN")
        return {"ok": False, "saved": 0, "new_srcs": [], "baseline_count": len(baseline)}

    # Tải ảnh
    saved = await download_flow_images(
        page,
        new_srcs=new_srcs,
        output_dir=output_dir,
        prefix=prefix,
        max_download=max_download,
        log_cb=log_cb,
    )

    return {
        "ok": saved > 0,
        "saved": saved,
        "new_srcs": new_srcs,
        "baseline_count": len(baseline),
        "download_limit": (int(max_download) if max_download is not None else None),
    }


# ════════════════════════════════════════════════════════════════════════════
#  TẢI ẢNH THEO API MAP (phương thức chính của pipeline hiện tại)
# ════════════════════════════════════════════════════════════════════════════

import hashlib  # noqa: E402 — import muộn để không làm nặng khi chỉ dùng hàm trên


def _sha256_bytes(data: bytes) -> str:
    """Tính SHA-256 của dữ liệu bytes."""
    return hashlib.sha256(data).hexdigest()


async def download_images_from_api_map(
    page,
    api_scene_map: dict,
    prompt_scene_order: list[int],
    output_dir: str,
    get_candidate_urls_fn,
    log_cb: Callable[[str, str], None] | None = None,
) -> tuple[int, set, list]:
    """
    Tải ảnh về disk dựa trên API scene map đã build (từ network intercept).

    Phương thức này là cách CHÍNH của pipeline:
    - Dùng URL từ API response (chất lượng cao nhất, không bị CORS).
    - Tên file: canh_001.png, canh_002.png, ...
    - Thử nhiều URL candidate nếu URL đầu fail.

    Args:
        page:                  Playwright page object (cần để gọi page.context.request.get).
        api_scene_map:         dict {scene_no: url_str} — từ build_api_scene_map_with_retry.
        prompt_scene_order:    list[int] — thứ tự scene theo prompt (vd: [1, 2, 3]).
        output_dir:            Thư mục lưu ảnh (tạo tự động nếu chưa có).
        get_candidate_urls_fn: Hàm (scene_no, url_str) → list[str] — expand nhiều URL candidate.
                               Thường là dreamina.get_scene_candidate_urls.
        log_cb:                Callback log tuỳ chọn, nhận (msg, level).

    Returns:
        (saved_count, saved_scenes_set, download_hash_records)
        - saved_count:         Số ảnh tải thành công.
        - saved_scenes_set:    Set scene_no đã tải xong.
        - download_hash_records: List dict chứa thông tin từng ảnh đã tải (để debug).

    Ví dụ gọi từ dreamina.py:
        from services.flow_image_service import download_images_from_api_map
        saved, saved_set, records = await download_images_from_api_map(
            page=page,
            api_scene_map=api_scene_map,
            prompt_scene_order=prompt_scene_order,
            output_dir=OUTPUT_DIR,
            get_candidate_urls_fn=get_scene_candidate_urls,
        )
    """
    def _cb(msg: str, level: str = "DBG") -> None:
        if callable(log_cb):
            log_cb(msg, level)
        else:
            _log(msg, level)

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    saved_count = 0
    saved_scenes: set[int] = set()
    hash_records: list[dict] = []

    for scene_no in prompt_scene_order:
        # Lấy danh sách URL candidate cho scene này
        raw_url = api_scene_map.get(scene_no, "")
        candidate_urls: list[str] = get_candidate_urls_fn(scene_no, raw_url)

        if not candidate_urls:
            _cb(f"  Thiếu URL API cho canh_{scene_no:03d}", "WARN")
            continue

        fname = f"canh_{scene_no:03d}.png"
        filepath = os.path.join(output_dir, fname)
        downloaded = False

        for src in candidate_urls:
            try:
                # Dùng Playwright request thay vì fetch() JS để tránh CORS
                resp = await page.context.request.get(src, timeout=30000)
                if not resp.ok:
                    continue

                body = await resp.body()
                # Lọc bỏ response quá nhỏ (placeholder / lỗi)
                if len(body) <= 5000:
                    continue

                with open(filepath, "wb") as f:
                    f.write(body)

                _cb(f"  {fname} ({len(body) // 1024}KB) [flow-api-map]", "OK")

                # Ghi hash để debug duplicate / corrupt
                sha = _sha256_bytes(body)
                hash_records.append({
                    "filename": fname,
                    "prompt_num": scene_no,
                    "prompt_index": scene_no,
                    "img_num": 1,
                    "src": src,
                    "method": "request.get_flow_api_map",
                    "size_bytes": len(body),
                    "sha256": sha,
                })

                saved_count += 1
                saved_scenes.add(scene_no)
                downloaded = True
                break  # Tải thành công → bỏ qua các candidate còn lại

            except Exception:
                continue  # Thử URL tiếp theo

        if not downloaded:
            _cb(f"  Không tải được canh_{scene_no:03d} theo API", "WARN")

    # Báo cáo tổng
    missing = [sc for sc in prompt_scene_order if sc not in saved_scenes]
    if missing:
        _cb(f"Cảnh chưa tải được: {missing}", "WARN")
    _cb(
        f"Tổng: {saved_count}/{len(prompt_scene_order)} ảnh đã tải",
        "OK" if saved_count == len(prompt_scene_order) else "WARN",
    )

    return saved_count, saved_scenes, hash_records
