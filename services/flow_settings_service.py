"""
Service auto setup panel setting trong Google Flow (image/video composer).

Mục tiêu:
- Gom logic click setting UI vào 1 nơi riêng để dễ bảo trì.
- Tránh nhét thêm nhiều thao tác UI vào dreamina.py.
- Có chế độ debug sâu để bắt đúng selector nhanh hơn, giảm số vòng sửa code.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable


# ── Đường dẫn động đến file config (luôn tìm tương đối từ vị trí file service này) ──
_SERVICE_DIR = Path(__file__).resolve().parent
_CONFIG_FILE = _SERVICE_DIR.parent / "config" / "flow_ui_settings.txt"


def _parse_range_or_float(val: str, default_min: float, default_max: float) -> tuple[float, float]:
    """
    Parse giá trị dạng "min - max" hoặc số đơn lẻ.

    Ví dụ:
        "1.5 - 3.5" -> (1.5, 3.5)
        "2.0"       -> (2.0, 2.0)  (số đơn lẻ, min = max)

    Nếu parse lỗi sẽ trả về giá trị mặc định.
    """
    val = str(val or "").strip()
    # Kiểm tra dạng "min - max"
    if "-" in val:
        parts = val.split("-")
        if len(parts) == 2:
            try:
                lo = float(parts[0].strip())
                hi = float(parts[1].strip())
                # Đảm bảo lo <= hi
                return (min(lo, hi), max(lo, hi))
            except Exception:
                pass
    # Dạng số đơn lẻ
    try:
        v = float(val)
        return (v, v)
    except Exception:
        pass
    return (default_min, default_max)


def load_flow_ui_settings() -> dict:
    """
    Đọc file config/flow_ui_settings.txt và trả về dict cài đặt.

    Định dạng file:
        key = value   (# là comment, bỏ qua dòng trống)

    Pipeline settings mới:
        scene_execution_mode  : "serial" | "pipeline"
        pipeline_max_in_flight: int - số prompt gửi song song tối đa
        pipeline_send_gap_sec : float hoặc "min-max" - khoảng nghỉ giữa 2 lần gửi
    """
    defaults: dict = {
        "auto_apply": False,
        "top_mode": "video",
        "secondary_mode": "thành phần",
        "aspect_ratio": "9:16",
        "multiplier": "x1",
        "model_name": "Nano Banana 2",
        "allow_model_alias_fallback": False,
        # Pipeline defaults an toàn
        "scene_execution_mode": "serial",
        "pipeline_max_in_flight": 2,
        "pipeline_send_gap_min": 1.5,  # kây - giá trị nhỏ nhất của khoảng nghỉ
        "pipeline_send_gap_max": 3.5,  # giây - giá trị lớn nhất của khoảng nghỉ
    }

    if not _CONFIG_FILE.exists():
        return dict(defaults)

    result = dict(defaults)
    try:
        for raw_line in _CONFIG_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.split("#")[0].strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().lower()
            val = val.strip()
            if key == "auto_apply":
                result["auto_apply"] = val.lower() in {"true", "1", "yes"}
            elif key == "top_mode":
                result["top_mode"] = val
            elif key == "secondary_mode":
                result["secondary_mode"] = val
            elif key == "aspect_ratio":
                result["aspect_ratio"] = val
            elif key == "multiplier":
                result["multiplier"] = val
            elif key == "model_name":
                result["model_name"] = val
            elif key == "allow_model_alias_fallback":
                result["allow_model_alias_fallback"] = val.lower() in {"true", "1", "yes"}
            # — Pipeline settings mới —
            elif key == "scene_execution_mode":
                # Chỉ chấp nhận "serial" hoặc "pipeline", lấy lằn về serial nếu không nhận ra.
                mode = val.lower()
                result["scene_execution_mode"] = mode if mode in {"serial", "pipeline"} else "serial"
            elif key == "pipeline_max_in_flight":
                try:
                    # Clamp trong khoảng hợp lệ 1-6 để tránh cấu hình quá cực.
                    result["pipeline_max_in_flight"] = max(1, min(6, int(val)))
                except Exception:
                    pass
            elif key == "pipeline_send_gap_sec":
                # Hỗ trợ cả dạng "1.5 - 3.5" lẫn số đơn lẻ "2.0"
                lo, hi = _parse_range_or_float(val, default_min=1.5, default_max=3.5)
                result["pipeline_send_gap_min"] = max(0.5, lo)
                result["pipeline_send_gap_max"] = max(result["pipeline_send_gap_min"], hi)
    except Exception as exc:
        import sys
        print(f"[flow_settings_service] Lỗi đọc {_CONFIG_FILE}: {exc}", file=sys.stderr)

    return result


def _normalize_ui_text(value: str) -> str:
    """
    Chuẩn hoá text để so khớp ổn định hơn:
    - lowercase
    - gộp nhiều khoảng trắng
    """
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _expand_model_aliases(model_name: str) -> list[str]:
    """
    Trả về danh sách alias model để tương thích khi UI đổi tên model.
    """
    base = str(model_name or "").strip()
    if not base:
        return []
    low = _normalize_ui_text(base)
    out: list[str] = [base]
    if "nano banana 2" in low:
        out.append("Nano Banana Pro")
    if "nano banana pro" in low:
        out.append("Nano Banana 2")
    # unique giữ thứ tự
    uniq: list[str] = []
    seen: set[str] = set()
    for item in out:
        key = _normalize_ui_text(item)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def _model_catalog_by_mode(top_mode: str) -> dict:
    """
    Trả về catalog model theo mode hiện tại.

    Lý do cần tách:
    - UI Hình ảnh và Video có dropdown model khác nhau.
    - Nếu dùng chung 1 list model sẽ dễ click nhầm hoặc fail không rõ nguyên nhân.
    """
    mode_low = _normalize_ui_text(top_mode)
    image_models = [
        "Nano Banana Pro",
        "Nano Banana 2",
        "Imagen 4",
    ]
    video_models = [
        "Veo 3.1 - Fast",
        "Veo 3.1 - Quality",
        "Veo 3.1 - Lite",
        "Veo 3.1 - Fast [Lower Priority]",
        "Veo 3.1 - Lite [Lower Priority]",
    ]
    is_video = mode_low == "video"
    return {
        "mode": "video" if is_video else "image",
        "allowed": video_models if is_video else image_models,
        "default": ("Veo 3.1 - Fast" if is_video else "Nano Banana Pro"),
    }


def _ratio_catalog_by_mode(top_mode: str) -> dict:
    """
    Trả về danh sách aspect ratio hợp lệ theo mode.

    Thực tế UI:
    - Hình ảnh: 16:9 | 4:3 | 1:1 | 3:4 | 9:16
    - Video: chỉ có 9:16 và 16:9
    """
    mode_low = _normalize_ui_text(top_mode)
    image_ratios = ["16:9", "4:3", "1:1", "3:4", "9:16"]
    video_ratios = ["9:16", "16:9"]
    is_video = mode_low == "video"
    return {
        "mode": "video" if is_video else "image",
        "allowed": video_ratios if is_video else image_ratios,
        "default": ("16:9" if is_video else "1:1"),
    }


def _resolve_ratio_for_mode(aspect_ratio: str, top_mode: str) -> tuple[str, list[str], str]:
    """
    Chuẩn hoá aspect ratio theo mode hiện tại.

    Trả về:
    - resolved_ratio: ratio cuối cùng sẽ dùng để click
    - labels: danh sách label dùng cho step click/check
    - warn: cảnh báo nếu ratio config không phù hợp mode
    """
    catalog = _ratio_catalog_by_mode(top_mode)
    allowed = [str(x) for x in (catalog.get("allowed") or [])]
    default_ratio = str(catalog.get("default") or "")
    mode_name = str(catalog.get("mode") or "")

    requested = str(aspect_ratio or "").strip()
    if not requested:
        return default_ratio, [default_ratio], f"aspect_ratio rỗng, tự dùng '{default_ratio}' cho mode '{mode_name}'."

    # Chuẩn hoá kiểu viết có khoảng trắng.
    requested_norm = requested.replace(" ", "")
    allowed_norm = [x.replace(" ", "") for x in allowed]
    if requested_norm in allowed_norm:
        idx = allowed_norm.index(requested_norm)
        resolved = allowed[idx]
        return resolved, [resolved], ""

    warn = (
        f"aspect_ratio='{requested}' không thuộc mode '{mode_name}'. "
        f"Tự fallback sang '{default_ratio}'."
    )
    return default_ratio, [default_ratio], warn


def _expand_model_aliases_for_mode(model_name: str, top_mode: str) -> tuple[str, list[str], str]:
    """
    Chuẩn hoá model user nhập theo mode, trả về:
    - resolved_model: model cuối cùng sẽ cố chọn
    - aliases: danh sách label dùng để match/click
    - warn: warning text (nếu model nhập không phù hợp mode)
    """
    catalog = _model_catalog_by_mode(top_mode)
    allowed = catalog["allowed"]
    default_model = str(catalog["default"])
    mode_name = str(catalog["mode"])
    requested = str(model_name or "").strip()
    warn = ""

    # Nếu user để trống model -> tự dùng default của mode hiện tại.
    if not requested:
        resolved = default_model
    else:
        resolved = requested

    aliases = _expand_model_aliases(resolved)

    # Bổ sung alias đặc thù để match ổn định khi UI có suffix động.
    low_resolved = _normalize_ui_text(resolved)
    if "veo 3.1 - fast" in low_resolved and "lower priority" not in low_resolved:
        aliases.extend(["Veo 3.1 - Fast [Lower Priority]"])
    if "veo 3.1 - lite" in low_resolved and "lower priority" not in low_resolved:
        aliases.extend(["Veo 3.1 - Lite [Lower Priority]"])

    # Nếu model user nhập không thuộc mode hiện tại, tự fallback về default để tránh fail.
    aliases_norm = [_normalize_ui_text(x) for x in aliases]
    allowed_norm = [_normalize_ui_text(x) for x in allowed]
    in_mode = any(a in b or b in a for a in aliases_norm for b in allowed_norm)
    if not in_mode:
        warn = (
            f"model_name='{requested}' không thuộc mode '{mode_name}'. "
            f"Tự fallback sang '{default_model}'."
        )
        resolved = default_model
        aliases = _expand_model_aliases(resolved)
        if _normalize_ui_text(resolved).startswith("veo 3.1 - fast"):
            aliases.append("Veo 3.1 - Fast [Lower Priority]")
        if _normalize_ui_text(resolved).startswith("veo 3.1 - lite"):
            aliases.append("Veo 3.1 - Lite [Lower Priority]")

    # unique giữ thứ tự
    uniq: list[str] = []
    seen: set[str] = set()
    for item in aliases:
        key = _normalize_ui_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(item)

    return resolved, uniq, warn


def _ui_text_token_match(text: str, label: str) -> bool:
    """
    So khớp text theo token boundary để tránh match sai kiểu container dài.
    """
    t = _normalize_ui_text(text)
    lb = _normalize_ui_text(label)
    if not t or not lb:
        return False
    if t == lb:
        return True
    if t.startswith(lb + " "):
        return True
    if t.endswith(" " + lb):
        return True
    if f" {lb} " in t:
        return True
    return False


def _ensure_debug_dir(debug_dir: str | None) -> Path | None:
    """
    Tạo thư mục debug nếu user bật debug.
    """
    if not debug_dir:
        return None
    path = Path(debug_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _dump_debug_json(debug_root: Path | None, filename: str, payload: dict | list) -> None:
    """
    Ghi file JSON debug theo UTF-8 để dễ đọc.
    """
    if not debug_root:
        return
    out = debug_root / filename
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


async def _dump_debug_screenshot(page, debug_root: Path | None, filename: str) -> str:
    """
    Chụp screenshot debug trước/sau mỗi bước.
    """
    if not debug_root:
        return ""
    out = debug_root / filename
    try:
        await page.screenshot(path=str(out), full_page=True)
        return str(out)
    except Exception:
        return ""


async def _collect_visible_clickable_snapshot(page) -> dict:
    """
    Snapshot chi tiết các node clickable đang hiển thị tại thời điểm gọi.

    Trả về:
    - visible_clickables: danh sách node có text/aria
    - selected_like_texts: text đang có dấu hiệu selected/active
    - url/title: context để debug
    """
    try:
        data = await page.evaluate(
            """
            () => {
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const toBool = (v) => String(v || '').toLowerCase();
              const selectedHints = [];
              const rows = [];

              const nodes = Array.from(document.querySelectorAll(
                'button,[role="button"],[role="tab"],[role="option"],label,div,span,[aria-label],[data-testid]'
              ));

              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || 1) === 0) continue;

                const txt = norm(el.innerText || el.textContent || '');
                const aria = norm(el.getAttribute('aria-label') || '');
                const role = norm(el.getAttribute('role') || '');
                const cls = norm(el.className || '');
                const ds = el.dataset || {};
                const dataState = norm(ds.state || '');
                const dataSelected = norm(ds.selected || '');
                const ariaSelected = toBool(el.getAttribute('aria-selected'));
                const ariaPressed = toBool(el.getAttribute('aria-pressed'));

                if (!txt && !aria) continue;

                const selectedLike = (
                  ariaSelected === 'true'
                  || ariaPressed === 'true'
                  || dataState === 'active'
                  || dataState === 'on'
                  || dataSelected === 'true'
                  || cls.includes('active')
                  || cls.includes('selected')
                  || cls.includes('checked')
                  || cls.includes('current')
                );

                const combined = norm((txt + ' ' + aria).trim());
                if (selectedLike && combined) selectedHints.push(combined);

                rows.push({
                  text: txt.slice(0, 120),
                  aria_label: aria.slice(0, 120),
                  role: role.slice(0, 40),
                  tag: String(el.tagName || '').toLowerCase(),
                  class_name: cls.slice(0, 200),
                  data_state: dataState,
                  data_selected: dataSelected,
                  aria_selected: ariaSelected,
                  aria_pressed: ariaPressed,
                  selected_like: selectedLike,
                  rect: {
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                  },
                });
              }

              return {
                url: String(location.href || ''),
                title: String(document.title || ''),
                visible_clickables: rows.slice(0, 400),
                selected_like_texts: Array.from(new Set(selectedHints)).slice(0, 200),
                counts: {
                  clickable_total: rows.length,
                  selected_hint_total: Array.from(new Set(selectedHints)).length,
                },
              };
            }
            """
        )
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        return {"error": f"collect_visible_clickable_snapshot: {exc}"}


async def _collect_panel_subtree_snapshot(page) -> dict:
    """
    Snapshot riêng DOM subtree của panel setting đang mở.

    Dùng để debug chính xác:
    - panel có mở đúng chưa
    - trong panel đang có những nút/text nào trước mỗi bước click
    """
    scope = await _get_generation_settings_panel_rect(page)
    if not scope:
        return {"has_panel": False, "scope": None, "items": [], "container_html": ""}
    try:
        snap = await page.evaluate(
            """
            (scope) => {
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const nodes = Array.from(document.querySelectorAll('div,section,article,aside,form'));
              const inScope = (r) => {
                if (!scope) return false;
                const cx = r.x + (r.width / 2);
                const cy = r.y + (r.height / 2);
                return (
                  cx >= scope.x &&
                  cx <= (scope.x + scope.w) &&
                  cy >= scope.y &&
                  cy <= (scope.y + scope.h)
                );
              };

              // Tìm container gần nhất bao phủ đúng vùng panel.
              let container = null;
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                if (!inScope(r)) continue;
                const area = r.width * r.height;
                // Container đủ lớn để chứa control, nhưng không lấy full page.
                if (area < 30000 || area > 450000) continue;
                const txt = norm(el.innerText || el.textContent || '');
                if (!txt) continue;
                if (!(txt.includes('video') || txt.includes('hình ảnh') || txt.includes('16:9') || txt.includes('9:16'))) {
                  continue;
                }
                container = el;
                break;
              }

              const root = container || document.body;
              const pick = Array.from(root.querySelectorAll('button,[role="button"],[role="tab"],[role="option"],label,div,span'));
              const rows = [];
              for (const el of pick) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                if (!inScope(r)) continue;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                const text = norm(el.innerText || el.textContent || '');
                const aria = norm(el.getAttribute('aria-label') || '');
                if (!text && !aria) continue;
                rows.push({
                  text: text.slice(0, 180),
                  aria_label: aria.slice(0, 140),
                  role: norm(el.getAttribute('role') || '').slice(0, 40),
                  tag: String(el.tagName || '').toLowerCase(),
                  class_name: norm(el.className || '').slice(0, 160),
                  rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
                });
              }

              return {
                has_panel: true,
                scope,
                container_html: String((container && container.outerHTML) || '').slice(0, 15000),
                items: rows.slice(0, 500),
              };
            }
            """,
            scope,
        )
        if isinstance(snap, dict):
            return snap
    except Exception as exc:
        return {"has_panel": False, "scope": scope, "error": str(exc), "items": [], "container_html": ""}
    return {"has_panel": False, "scope": scope, "items": [], "container_html": ""}


async def _is_any_label_selected(page, labels: list[str]) -> bool:
    """
    Kiểm tra nhanh xem label có đang ở trạng thái selected/active hay chưa.
    """
    labels_norm = [_normalize_ui_text(x) for x in labels if str(x or "").strip()]
    if not labels_norm:
        return False
    snap = await _collect_visible_clickable_snapshot(page)
    selected_texts = [str(x or "") for x in (snap.get("selected_like_texts") or [])]
    selected_norm = [_normalize_ui_text(x) for x in selected_texts]
    for lb in labels_norm:
        for st in selected_norm:
            if st == lb or lb in st or st in lb:
                return True
    return False


async def _click_by_visible_text(
    page,
    labels: list[str],
    timeout_ms: int = 1200,
    debug_row: dict | None = None,
    exact_only: bool = False,
    prefer_bottom: bool = False,
    require_panel_scope: bool = False,
) -> bool:
    """
    Click 1 element đang hiển thị theo text.

    Cách làm:
    - Ưu tiên text khớp chính xác qua Playwright.
    - Fallback text contains qua Playwright.
    - Cuối cùng fallback JS cho UI custom.
    """
    labels_norm = [_normalize_ui_text(x) for x in labels if str(x or "").strip()]
    if not labels_norm:
        if debug_row is not None:
            debug_row["click_attempt"] = {"reason": "empty_labels"}
        return False

    click_trace: dict = {
        "labels_raw": labels,
        "labels_norm": labels_norm,
        "playwright_exact": [],
        "playwright_contains": [],
        "js_fallback": {},
        "panel_scope": {},
    }

    panel_scope: dict | None = None
    if require_panel_scope:
        panel_scope = await _get_generation_settings_panel_rect(page)
        click_trace["panel_scope"] = panel_scope or {}
        if not panel_scope:
            if debug_row is not None:
                debug_row["click_attempt"] = click_trace
                debug_row["clicked_by"] = ""
            return False

    # 1) Exact match
    for raw in labels:
        text = str(raw or "").strip()
        if not text:
            continue
        info = {"label": text, "count": 0, "clicked": False, "error": ""}
        try:
            target = page.get_by_text(text, exact=True).first
            info["count"] = int(await target.count())
            if info["count"] > 0 and await target.is_visible(timeout=timeout_ms):
                await target.click(timeout=timeout_ms)
                await asyncio.sleep(0.2)
                info["clicked"] = True
                click_trace["playwright_exact"].append(info)
                if debug_row is not None:
                    debug_row["click_attempt"] = click_trace
                    debug_row["clicked_by"] = "playwright_exact"
                return True
        except Exception as exc:
            info["error"] = str(exc)
        click_trace["playwright_exact"].append(info)

    # 1.1) Token-boundary regex match (hỗ trợ text có prefix icon kiểu 'videocam Video').
    for raw in labels:
        text = str(raw or "").strip()
        if not text:
            continue
        info = {"label": text, "count": 0, "clicked": False, "error": ""}
        try:
            patt = re.compile(rf"(^|\\s){re.escape(text)}(\\s|$)", re.IGNORECASE)
            target = page.get_by_text(patt).first
            info["count"] = int(await target.count())
            if info["count"] > 0 and await target.is_visible(timeout=timeout_ms):
                await target.click(timeout=timeout_ms)
                await asyncio.sleep(0.2)
                info["clicked"] = True
                click_trace["playwright_exact"].append(info)
                if debug_row is not None:
                    debug_row["click_attempt"] = click_trace
                    debug_row["clicked_by"] = "playwright_token_regex"
                return True
        except Exception as exc:
            info["error"] = str(exc)
        click_trace["playwright_exact"].append(info)

    # 2) Contains match (chỉ bật khi không strict exact).
    if not exact_only:
        for raw in labels:
            text = str(raw or "").strip()
            if not text:
                continue
            info = {"label": text, "count": 0, "clicked": False, "error": ""}
            try:
                target = page.get_by_text(text, exact=False).first
                info["count"] = int(await target.count())
                if info["count"] > 0 and await target.is_visible(timeout=timeout_ms):
                    await target.click(timeout=timeout_ms)
                    await asyncio.sleep(0.2)
                    info["clicked"] = True
                    click_trace["playwright_contains"].append(info)
                    if debug_row is not None:
                        debug_row["click_attempt"] = click_trace
                        debug_row["clicked_by"] = "playwright_contains"
                    return True
            except Exception as exc:
                info["error"] = str(exc)
            click_trace["playwright_contains"].append(info)

    # 3) JS fallback
    try:
        js_result = await page.evaluate(
            """
            ({ labels, exactOnly, preferBottom, scope }) => {
              const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
              const banned = ['generate', 'create video', 'tạo video', 'tao video'];
              const rows = [];
              const nodes = Array.from(document.querySelectorAll(
                'button,[role="button"],[role="tab"],[role="option"],label,div,span'
              ));
              const vh = window.innerHeight || 0;
              const inScope = (r) => {
                if (!scope) return true;
                const cx = r.x + (r.width / 2);
                const cy = r.y + (r.height / 2);
                return (
                  cx >= scope.x &&
                  cx <= (scope.x + scope.w) &&
                  cy >= scope.y &&
                  cy <= (scope.y + scope.h)
                );
              };
              const lbMatch = (txt, lb) => {
                if (!txt || !lb) return false;
                if (txt === lb) return true;
                if (txt.startsWith(lb + ' ')) return true;
                if (txt.endsWith(' ' + lb)) return true;
                if (txt.includes(' ' + lb + ' ')) return true;
                return false;
              };
              const containsLoose = (txt, lb) => {
                if (!txt || !lb) return false;
                return txt.includes(lb) || lb.includes(txt);
              };
              const scoreNode = (row, lb, exactPhase) => {
                let s = 0;
                const role = row.role || '';
                const tag = row.tag || '';
                const txt = row.txt || '';
                const isButtonLike = tag === 'button' || role === 'button' || role === 'tab' || role === 'option' || row.className.includes('button');
                if (isButtonLike) s += 12000;
                if (role === 'tab') s += 5000;
                if (role === 'option') s += 3500;
                if (txt === lb) s += 9000;
                if (txt.startsWith(lb + ' ') || txt.endsWith(' ' + lb) || txt.includes(' ' + lb + ' ')) s += 4500;
                if (!exactPhase && containsLoose(txt, lb)) s += 1200;
                // Ưu tiên text ngắn gọn (nút thật) thay vì container gom nhiều text.
                s -= Math.max(0, txt.length - lb.length) * 6;
                const tokenCount = txt.split(' ').filter(Boolean).length;
                if (tokenCount > 8) s -= 4000;
                return s;
              };

              let clickedBy = '';
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                if (preferBottom && r.y < vh * 0.45) continue;
                if (!inScope(r)) continue;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden') continue;

                const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                if (!txt) continue;
                if (banned.some((k) => txt.includes(k))) continue;

                rows.push({
                  txt: txt.slice(0, 180),
                  tag: String(el.tagName || '').toLowerCase(),
                  role: norm(el.getAttribute('role') || ''),
                  className: norm(el.className || '').slice(0, 160),
                  nodeIndex: rows.length,
                });
              }

              const clickBest = (matcher, phaseName) => {
                let best = null;
                for (const lb of labels) {
                  for (const n of nodes) {
                    const r = n.getBoundingClientRect();
                    if (!r || r.width <= 0 || r.height <= 0) continue;
                    if (preferBottom && r.y < vh * 0.45) continue;
                    if (!inScope(r)) continue;
                    const style = window.getComputedStyle(n);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                    const txt = norm(n.innerText || n.textContent || n.getAttribute('aria-label') || '');
                    if (!txt) continue;
                    if (banned.some((k) => txt.includes(k))) continue;
                    if (!matcher(txt, lb)) continue;
                    const row = {
                      txt,
                      tag: String(n.tagName || '').toLowerCase(),
                      role: norm(n.getAttribute('role') || ''),
                      className: norm(n.className || ''),
                    };
                    const sc = scoreNode(row, lb, phaseName === 'exact');
                    if (!best || sc > best.score) best = { el: n, row, label: lb, score: sc };
                  }
                }
                if (best && best.el) {
                  try {
                    best.el.click();
                    return {
                      clicked: true,
                      clicked_by: phaseName,
                      clicked_label: best.label,
                      clicked_text: best.row.txt,
                      clicked_role: best.row.role,
                      clicked_tag: best.row.tag,
                      score: best.score,
                    };
                  } catch (_) {}
                }
                return { clicked: false };
              };

              // ưu tiên exact/token boundary trên node thực.
              const exactPick = clickBest((txt, lb) => lbMatch(txt, lb), 'js_token_match');
              if (exactPick.clicked) {
                return { ...exactPick, sample_rows: rows.slice(0, 120) };
              }

              // fallback contains
              if (!exactOnly) {
                const loosePick = clickBest((txt, lb) => containsLoose(txt, lb), 'js_contains');
                if (loosePick.clicked) {
                  return { ...loosePick, sample_rows: rows.slice(0, 120) };
                }
              }

              return { clicked: false, clicked_by: '', sample_rows: rows.slice(0, 120) };
            }
            """,
            {
                "labels": labels_norm,
                "exactOnly": exact_only,
                "preferBottom": prefer_bottom,
                "scope": panel_scope,
            },
        )
        click_trace["js_fallback"] = js_result if isinstance(js_result, dict) else {}
        if isinstance(js_result, dict) and js_result.get("clicked"):
            await asyncio.sleep(0.2)
            if debug_row is not None:
                debug_row["click_attempt"] = click_trace
                debug_row["clicked_by"] = str(js_result.get("clicked_by") or "js_fallback")
            return True
    except Exception as exc:
        click_trace["js_fallback"] = {"error": str(exc)}

    if debug_row is not None:
        debug_row["click_attempt"] = click_trace
        debug_row["clicked_by"] = ""
    return False


async def _click_tab_label_by_mouse_center(
    page,
    labels: list[str],
    debug_row: dict | None = None,
    require_panel_scope: bool = True,
) -> bool:
    """
    Fallback mạnh cho tab (ratio/multiplier):
    - Tìm đúng button role=tab theo label
    - Click bằng mouse tại tâm nút để mô phỏng user click thật

    Lý do:
    Một số UI dùng Radix/overlay có thể bỏ qua `element.click()` trong JS fallback.
    """
    labels_norm = [_normalize_ui_text(x) for x in labels if str(x or "").strip()]
    if not labels_norm:
        return False
    panel_scope = await _get_generation_settings_panel_rect(page) if require_panel_scope else None

    try:
        hit = await page.evaluate(
            """
            ({ labels, scope }) => {
              const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
              const lbMatch = (txt, lb) => {
                if (!txt || !lb) return false;
                if (txt === lb) return true;
                if (txt.startsWith(lb + ' ')) return true;
                if (txt.endsWith(' ' + lb)) return true;
                if (txt.includes(' ' + lb + ' ')) return true;
                return false;
              };
              const inScope = (r) => {
                if (!scope) return true;
                const cx = r.x + (r.width / 2);
                const cy = r.y + (r.height / 2);
                return (
                  cx >= scope.x &&
                  cx <= (scope.x + scope.w) &&
                  cy >= scope.y &&
                  cy <= (scope.y + scope.h)
                );
              };

              const tabs = Array.from(document.querySelectorAll("button[role='tab']"));
              const cands = [];
              for (const el of tabs) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                if (!inScope(r)) continue;
                const st = window.getComputedStyle(el);
                if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
                const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                if (!txt) continue;
                for (const lb of labels) {
                  if (!lbMatch(txt, lb)) continue;
                  const score = 10000 - Math.max(0, txt.length - lb.length) * 10;
                  cands.push({
                    text: txt.slice(0, 180),
                    label: lb,
                    score,
                    x: Math.round(r.x + r.width / 2),
                    y: Math.round(r.y + r.height / 2),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    aria_selected: String(el.getAttribute('aria-selected') || ''),
                    aria_controls: String(el.getAttribute('aria-controls') || ''),
                  });
                }
              }
              cands.sort((a, b) => (b.score || 0) - (a.score || 0));
              return cands.length ? cands[0] : null;
            }
            """,
            {"labels": labels_norm, "scope": panel_scope},
        )
        if not isinstance(hit, dict):
            return False
        x = int(hit.get("x", 0))
        y = int(hit.get("y", 0))
        if x <= 0 or y <= 0:
            return False
        await page.mouse.click(x, y)
        await asyncio.sleep(0.25)
        if debug_row is not None:
            debug_row["mouse_tab_fallback"] = {
                "clicked": True,
                "target": hit,
                "panel_scope": panel_scope,
            }
        return True
    except Exception as exc:
        if debug_row is not None:
            debug_row["mouse_tab_fallback"] = {
                "clicked": False,
                "error": str(exc),
                "panel_scope": panel_scope,
            }
        return False


async def _open_model_dropdown_if_needed(page, debug_row: dict | None = None) -> bool:
    """
    Mở dropdown model trong panel setting.
    """
    selectors = [
        "[role='combobox']",
        "button[aria-haspopup='listbox']",
        "[aria-expanded='false'][role='button']",
        "[aria-haspopup='menu']",
    ]
    panel_scope = await _get_generation_settings_panel_rect(page)
    attempts: list[dict] = []
    for sel in selectors:
        info = {"selector": sel, "count": 0, "clicked": False, "error": ""}
        try:
            locator = page.locator(sel)
            count = int(await locator.count())
            info["count"] = count
            for idx in range(min(count, 10)):
                el = locator.nth(idx)
                if not await el.is_visible(timeout=300):
                    continue
                box = await el.bounding_box()
                if not box:
                    continue
                cx = float(box.get("x", 0.0)) + float(box.get("width", 0.0)) / 2.0
                cy = float(box.get("y", 0.0)) + float(box.get("height", 0.0)) / 2.0
                if panel_scope:
                    in_scope = (
                        cx >= float(panel_scope.get("x", 0))
                        and cx <= float(panel_scope.get("x", 0)) + float(panel_scope.get("w", 0))
                        and cy >= float(panel_scope.get("y", 0))
                        and cy <= float(panel_scope.get("y", 0)) + float(panel_scope.get("h", 0))
                    )
                    if not in_scope:
                        continue
                await el.click(timeout=900)
                await asyncio.sleep(0.25)
                info["clicked"] = True
                info["picked_index"] = idx
                info["picked_center"] = {"x": round(cx), "y": round(cy)}
                attempts.append(info)
                if debug_row is not None:
                    debug_row["open_dropdown_attempts"] = attempts
                    debug_row["dropdown_opened_by"] = f"selector:{sel}[{idx}]"
                return True
        except Exception as exc:
            info["error"] = str(exc)
        attempts.append(info)

    # fallback click theo text model phổ biến
    fallback_debug: dict = {}
    fallback_ok = await _click_by_visible_text(
        page,
        labels=[
            "Veo",
            "Veo 3.1 - Fast",
            "Veo 3.1 - Quality",
            "Veo 3.1 - Lite",
            "Nano Banana",
            "Nano Banana Pro",
            "Nano Banana 2",
            "Imagen 4",
        ],
        debug_row=fallback_debug,
        exact_only=False,
        prefer_bottom=True,
        require_panel_scope=True,
    )
    if debug_row is not None:
        debug_row["open_dropdown_attempts"] = attempts
        debug_row["open_dropdown_fallback"] = fallback_debug
        debug_row["dropdown_opened_by"] = "text_fallback" if fallback_ok else ""
    return fallback_ok


async def _get_current_model_label(page) -> str:
    """
    Đọc label model đang hiển thị trên nút dropdown.
    """
    panel_scope = await _get_generation_settings_panel_rect(page)
    try:
        label = await page.evaluate(
            """
            (scope) => {
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const inScope = (r) => {
                if (!scope) return true;
                const cx = r.x + (r.width / 2);
                const cy = r.y + (r.height / 2);
                return (
                  cx >= scope.x &&
                  cx <= (scope.x + scope.w) &&
                  cy >= scope.y &&
                  cy <= (scope.y + scope.h)
                );
              };
              const nodes = Array.from(document.querySelectorAll(
                "button[aria-haspopup='menu'],button[aria-haspopup='listbox'],[role='combobox'],button"
              ));
              let best = null;
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                if (!inScope(r)) continue;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                if (!txt) continue;
                const low = txt.toLowerCase();
                const isModelLike = low.includes('banana') || low.includes('veo') || low.includes('imagen');
                if (!isModelLike) continue;
                const score = (low.includes('arrow_drop_down') ? 2000 : 0) + Math.round(r.width * r.height);
                if (!best || score > best.score) {
                  best = { text: txt, score };
                }
              }
              return best ? String(best.text || '') : '';
            }
            """,
            panel_scope,
        )
        return str(label or "").strip()
    except Exception:
        return ""


async def _collect_visible_model_options(page) -> list[str]:
    """
    Lấy danh sách option model đang hiển thị (sau khi mở dropdown).
    """
    panel_scope = await _get_generation_settings_panel_rect(page)
    try:
        rows = await page.evaluate(
            """
            (scope) => {
              const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const inScope = (r) => {
                if (!scope) return true;
                const cx = r.x + (r.width / 2);
                const cy = r.y + (r.height / 2);
                // dropdown có thể bung ra ngoài panel, nên nới biên một chút
                return (
                  cx >= (scope.x - 40) &&
                  cx <= (scope.x + scope.w + 40) &&
                  cy >= (scope.y - 220) &&
                  cy <= (scope.y + scope.h + 220)
                );
              };
              const nodes = Array.from(document.querySelectorAll(
                "[role='menuitem'],[role='option'],button,div,span"
              ));
              const out = [];
              const seen = new Set();
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                if (!inScope(r)) continue;
                const st = window.getComputedStyle(el);
                if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
                const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                if (!txt) continue;
                const low = txt.toLowerCase();
                const hit = low.includes('banana') || low.includes('veo') || low.includes('imagen');
                if (!hit) continue;
                if (txt.length > 120) continue;
                if (seen.has(txt)) continue;
                seen.add(txt);
                out.push(txt);
              }
              return out.slice(0, 30);
            }
            """,
            panel_scope,
        )
        if isinstance(rows, list):
            return [str(x) for x in rows if str(x).strip()]
    except Exception:
        pass
    return []


async def _click_model_menuitem_by_text(
    page,
    labels: list[str],
    debug_row: dict | None = None,
) -> bool:
    """
    Click chính xác vào 1 model option trong dropdown đã mở.

    Khác với _click_by_visible_text:
    - Chỉ scan menuitem/option (không quét toàn page).
    - Nới rộng vùng scan ra ngoài panel vì dropdown bung ra ngoài panel scope.
    - Loại bỏ emoji trước khi so text để match "Nano Banana 2" với "🍌 Nano Banana 2".
    """
    labels_norm = [_normalize_ui_text(x) for x in labels if str(x or "").strip()]
    if not labels_norm:
        return False

    panel_scope = await _get_generation_settings_panel_rect(page)

    try:
        hit = await page.evaluate(
            """
            ({ labels, scope }) => {
              const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
              // Loại bỏ emoji để so sánh text thuần
              const stripEmoji = (s) => s.replace(/[\\u{1F300}-\\u{1FAD6}\\u{1F600}-\\u{1F64F}\\u{1F680}-\\u{1F6FF}\\u{2600}-\\u{26FF}\\u{2700}-\\u{27BF}\\u{FE00}-\\u{FE0F}\\u{1F900}-\\u{1F9FF}\\u{200D}\\u{20E3}]/gu, '').trim();

              // Nới biên rộng để bắt dropdown bung ra ngoài panel
              const inExpandedScope = (r) => {
                if (!scope) return true;
                const cx = r.x + (r.width / 2);
                const cy = r.y + (r.height / 2);
                return (
                  cx >= (scope.x - 80) &&
                  cx <= (scope.x + scope.w + 80) &&
                  cy >= (scope.y - 300) &&
                  cy <= (scope.y + scope.h + 300)
                );
              };

              // Ưu tiên menuitem, option, rồi button
              const selectors = "[role='menuitem'],[role='option'],button";
              const nodes = Array.from(document.querySelectorAll(selectors));
              const candidates = [];

              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                if (!inExpandedScope(r)) continue;
                const st = window.getComputedStyle(el);
                if (!st || st.display === 'none' || st.visibility === 'hidden') continue;

                const rawText = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                if (!rawText) continue;

                // Chỉ xét node có chứa keyword model
                const low = rawText.toLowerCase();
                const isModelLike = low.includes('banana') || low.includes('veo') || low.includes('imagen');
                if (!isModelLike) continue;

                // So text sau khi strip emoji
                const cleanText = norm(stripEmoji(rawText));
                const role = norm(el.getAttribute('role') || '');
                const tag = String(el.tagName || '').toLowerCase();

                for (const lb of labels) {
                  // Match exact hoặc clean text match
                  const isExact = (cleanText === lb || rawText === lb);
                  const isContains = (cleanText.includes(lb) || rawText.includes(lb));
                  if (!isExact && !isContains) continue;

                  // Tính điểm: ưu tiên exact, text ngắn, menuitem
                  let score = 0;
                  if (isExact) score += 20000;
                  if (isContains) score += 5000;
                  if (role === 'menuitem') score += 10000;
                  if (role === 'option') score += 8000;
                  if (tag === 'button') score += 3000;
                  // Text ngắn hơn = nút thật, không phải container
                  score -= Math.max(0, rawText.length - lb.length) * 10;
                  // Phạt nặng nếu text quá dài (container gom)
                  if (rawText.split(' ').length > 8) score -= 8000;

                  candidates.push({
                    el,
                    rawText: rawText.slice(0, 120),
                    cleanText: cleanText.slice(0, 120),
                    label: lb,
                    role,
                    tag,
                    score,
                    cx: Math.round(r.x + r.width / 2),
                    cy: Math.round(r.y + r.height / 2),
                  });
                }
              }

              if (!candidates.length) {
                return { clicked: false, reason: 'no_candidates', candidates: [] };
              }

              // Sắp xếp theo score giảm dần
              candidates.sort((a, b) => b.score - a.score);
              const best = candidates[0];

              try {
                best.el.click();
                return {
                  clicked: true,
                  clicked_text: best.rawText,
                  clicked_clean: best.cleanText,
                  clicked_label: best.label,
                  clicked_role: best.role,
                  clicked_tag: best.tag,
                  score: best.score,
                  cx: best.cx,
                  cy: best.cy,
                  total_candidates: candidates.length,
                  top3: candidates.slice(0, 3).map(c => ({
                    text: c.rawText, clean: c.cleanText, role: c.role, tag: c.tag, score: c.score
                  })),
                };
              } catch (e) {
                return {
                  clicked: false,
                  reason: 'click_error: ' + String(e),
                  best_text: best.rawText,
                  best_cx: best.cx,
                  best_cy: best.cy,
                };
              }
            }
            """,
            {"labels": labels_norm, "scope": panel_scope},
        )

        if isinstance(hit, dict):
            if debug_row is not None:
                debug_row["model_menuitem_click"] = hit
            if hit.get("clicked"):
                await asyncio.sleep(0.3)
                return True

            # Fallback: nếu JS click fail, thử mouse click vào toạ độ trung tâm
            cx = int(hit.get("best_cx") or hit.get("cx") or 0)
            cy = int(hit.get("best_cy") or hit.get("cy") or 0)
            if cx > 0 and cy > 0:
                await page.mouse.click(cx, cy)
                await asyncio.sleep(0.3)
                if debug_row is not None:
                    debug_row["model_menuitem_mouse_fallback"] = {"cx": cx, "cy": cy}
                return True

    except Exception as exc:
        if debug_row is not None:
            debug_row["model_menuitem_error"] = str(exc)

    return False


async def _is_generation_settings_panel_expanded(page) -> bool:
    """
    Kiểm tra panel setting đã mở rộng chưa.

    Dấu hiệu mở rộng:
    - Có nút/tab "Video" hoặc "Hình ảnh"
    - hoặc có nhiều lựa chọn ratio (9:16, 16:9, 1:1...)
    """
    panel_rect = await _get_generation_settings_panel_rect(page)
    return bool(panel_rect)


async def _get_generation_settings_panel_rect(page) -> dict | None:
    """
    Tìm panel setting mở rộng thật sự (không nhầm pill compact).
    """
    try:
        rect = await page.evaluate(
            """
            () => {
              const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
              const vh = window.innerHeight || 0;
              const nodes = Array.from(document.querySelectorAll('div,section,article,aside,form'));
              const ratioKeys = ['16:9', '9:16', '4:3', '3:4', '1:1'];
              const multiKeys = ['x1', 'x2', 'x3', 'x4'];
              const out = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width < 180 || r.height < 120) continue;
                if (r.y < vh * 0.45) continue;
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                const txt = norm(el.innerText || el.textContent || '');
                if (!txt) continue;
                const hasImage = txt.includes('hình ảnh') || txt.includes(' image ');
                const hasVideo = txt.includes(' video ');
                if (!(hasImage && hasVideo)) continue;
                let ratioHits = 0;
                for (const k of ratioKeys) if (txt.includes(k)) ratioHits += 1;
                let multiHits = 0;
                for (const k of multiKeys) if (txt.includes(k)) multiHits += 1;
                if (ratioHits < 2 || multiHits < 2) continue;
                out.push({
                  x: Math.round(r.x),
                  y: Math.round(r.y),
                  w: Math.round(r.width),
                  h: Math.round(r.height),
                  area: Math.round(r.width * r.height),
                  ratioHits,
                  multiHits,
                });
              }
              if (!out.length) return null;
              out.sort((a, b) => {
                const sa = a.area + a.ratioHits * 10000 + a.multiHits * 10000;
                const sb = b.area + b.ratioHits * 10000 + b.multiHits * 10000;
                return sb - sa;
              });
              return out[0];
            }
            """
        )
        return rect if isinstance(rect, dict) else None
    except Exception:
        return None


async def _ensure_generation_settings_panel_open(page, debug_row: dict | None = None) -> bool:
    """
    Đảm bảo panel setting đang ở trạng thái mở rộng trước khi apply các option.
    """
    if await _is_generation_settings_panel_expanded(page):
        if debug_row is not None:
            debug_row["panel_open_already"] = True
        return True

    attempts: list[dict] = []

    async def _click_compact_pill_once(strategy_name: str) -> tuple[bool, dict]:
        """
        Tìm candidate pill tốt nhất, click theo toạ độ trung tâm,
        và trả về log chi tiết để debug.
        """
        detail: dict = {"strategy": strategy_name, "clicked": False, "error": "", "picked": None, "candidates": []}
        try:
            scan = await page.evaluate(
                """
                () => {
                  const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                  const vh = window.innerHeight || 0;
                  const nodes = Array.from(document.querySelectorAll('button,[role="button"],div,span'));
                  const rows = [];
                  for (const el of nodes) {
                    const r = el.getBoundingClientRect();
                    if (!r || r.width <= 0 || r.height <= 0) continue;
                    if (r.y < vh * 0.55) continue;
                    if (r.width > 360 || r.height > 90) continue;
                    if (r.width < 60 || r.height < 18) continue;
                    const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                    if (!txt) continue;
                    const hitModel = txt.includes('veo') || txt.includes('nano banana') || txt.includes('banana') || txt.includes('video') || txt.includes('image') || txt.includes('hình ảnh');
                    const hitRatio = txt.includes('16:9') || txt.includes('9:16') || txt.includes('crop_9_16') || txt.includes('crop_16_9');
                    const hitMulti = /\\bx\\s*[1-4]\\b/.test(txt);
                    const tokenCount = Number(hitModel) + Number(hitRatio) + Number(hitMulti);
                    if (tokenCount < 2) continue;
                    const tag = String(el.tagName || '').toLowerCase();
                    const role = norm(el.getAttribute('role') || '');
                    const className = norm(el.className || '');
                    const isButtonLike = tag === 'button' || role === 'button' || className.includes('button');
                    const area = r.width * r.height;
                    const score = (isButtonLike ? 100000 : 0) - Math.abs(area - 9000) + (tokenCount * 1000);
                    rows.push({
                      text: txt.slice(0, 180),
                      tag,
                      role,
                      className: className.slice(0, 140),
                      isButtonLike,
                      tokenCount,
                      area: Math.round(area),
                      score: Math.round(score),
                      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
                      center: { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) },
                    });
                  }
                  rows.sort((a, b) => (b.score || 0) - (a.score || 0));
                  return { candidates: rows.slice(0, 20), picked: rows.length ? rows[0] : null };
                }
                """
            )
            detail["candidates"] = (scan or {}).get("candidates", [])
            picked = (scan or {}).get("picked")
            detail["picked"] = picked
            if picked and isinstance(picked, dict):
                cx = int((picked.get("center") or {}).get("x", 0))
                cy = int((picked.get("center") or {}).get("y", 0))
                if cx > 0 and cy > 0:
                    await page.mouse.click(cx, cy)
                    await asyncio.sleep(0.35)
                    detail["clicked"] = True
                    return True, detail
            return False, detail
        except Exception as exc:
            detail["error"] = str(exc)
            return False, detail

    # Click pill compact nhiều vòng để mở panel (UI đôi khi cần 2 nhịp).
    for i in range(3):
        ok, info = await _click_compact_pill_once(f"compact_settings_pill_round_{i+1}")
        attempts.append(info)
        if ok and await _is_generation_settings_panel_expanded(page):
            if debug_row is not None:
                debug_row["panel_open_attempts"] = attempts
                debug_row["panel_opened_by"] = "compact_settings_pill"
            return True

    # Fallback cứng: click đúng nút pill trạng thái ở góc dưới phải (video/image + xN).
    try:
        info = {"strategy": "status_pill_direct_fallback", "clicked": False, "error": "", "picked": None, "candidates": []}
        scan = await page.evaluate(
            """
            () => {
              const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
              const vh = window.innerHeight || 0;
              const vw = window.innerWidth || 0;
              const nodes = Array.from(document.querySelectorAll('button,[role="button"],div,span'));
              const picks = [];
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r || r.width <= 0 || r.height <= 0) continue;
                if (r.y < vh * 0.62) continue;
                if (r.x < vw * 0.55) continue;
                if (r.width < 70 || r.width > 320 || r.height < 18 || r.height > 80) continue;
                const txt = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                if (!txt) continue;
                const hasMode = txt.includes('video') || txt.includes('hình ảnh') || txt.includes('image') || txt.includes('nano banana') || txt.includes('veo');
                const hasMulti = /\\bx\\s*[1-4]\\b/.test(txt);
                if (!(hasMode && hasMulti)) continue;
                const area = r.width * r.height;
                picks.push({
                  text: txt.slice(0, 180),
                  area: Math.round(area),
                  rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
                  center: { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) },
                });
              }
              picks.sort((a, b) => ((a.area || 0) - (b.area || 0)));
              return { candidates: picks.slice(0, 20), picked: picks.length ? picks[0] : null };
            }
            """
        )
        info["candidates"] = (scan or {}).get("candidates", [])
        info["picked"] = (scan or {}).get("picked")
        picked = info["picked"] or {}
        cx = int((picked.get("center") or {}).get("x", 0))
        cy = int((picked.get("center") or {}).get("y", 0))
        if cx > 0 and cy > 0:
            await page.mouse.click(cx, cy)
            await asyncio.sleep(0.35)
            info["clicked"] = True
        attempts.append(info)
        if info["clicked"] and await _is_generation_settings_panel_expanded(page):
            if debug_row is not None:
                debug_row["panel_open_attempts"] = attempts
                debug_row["panel_opened_by"] = "status_pill_direct_fallback"
            return True
    except Exception as exc:
        attempts.append({"strategy": "status_pill_direct_fallback", "clicked": False, "error": str(exc)})

    if debug_row is not None:
        debug_row["panel_open_attempts"] = attempts
        debug_row["panel_opened_by"] = ""
    return await _is_generation_settings_panel_expanded(page)


async def apply_flow_generation_settings_panel(
    page,
    top_mode: str = "video",
    secondary_mode: str = "",
    aspect_ratio: str = "16:9",
    multiplier: str = "x4",
    model_name: str = "Veo 3.1 - Fast",
    allow_model_alias_fallback: bool = False,
    log_cb: Callable[[str, str], None] | None = None,
    debug_dir: str | None = None,
) -> dict:
    """
    Auto setup panel giống ảnh mẫu trong composer.

    Request mà hàm này "gửi đi":
    - Chuỗi hành động UI click (tab, ratio, multiplier, model) trên browser.

    Response mà hàm này "nhận về":
    - dict kết quả gồm:
      - `ok`: có setup thành công cơ bản hay không
      - `applied`: trạng thái từng mục
      - `steps`: các bước đã click
      - `errors`: lỗi chi tiết nếu có
      - `debug`: metadata để phân tích nhanh khi fail
    """
    debug_root = _ensure_debug_dir(debug_dir)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    top_mode_low = _normalize_ui_text(top_mode)
    secondary_low = _normalize_ui_text(secondary_mode)
    ratio = str(aspect_ratio or "").strip()
    resolved_ratio, ratio_labels, ratio_warn = _resolve_ratio_for_mode(ratio, top_mode_low)
    ratio_catalog = _ratio_catalog_by_mode(top_mode_low)
    multi = str(multiplier or "").strip()
    model = str(model_name or "").strip()
    resolved_model, model_aliases, model_warn = _expand_model_aliases_for_mode(model, top_mode_low)
    model_catalog = _model_catalog_by_mode(top_mode_low)

    result: dict = {
        "ok": False,
        "applied": {
            "top_mode": False,
            "secondary_mode": True if not str(secondary_mode or "").strip() else False,
            "aspect_ratio": False,
            "multiplier": False,
            "model_name": False if resolved_model else True,
        },
        "steps": [],
        "errors": [],
        "inputs": {
            "top_mode": top_mode,
            "secondary_mode": secondary_mode,
            "aspect_ratio": aspect_ratio,
            "multiplier": multiplier,
            "model_name": model_name,
            "allow_model_alias_fallback": bool(allow_model_alias_fallback),
        },
        "debug": {
            "run_id": run_id,
            "debug_dir": str(debug_root) if debug_root else "",
            "step_traces": [],
            "screenshots": {},
            "ratio_resolution": {
                "requested": ratio,
                "resolved": resolved_ratio,
                "labels": ratio_labels,
                "mode_catalog": ratio_catalog,
                "warning": ratio_warn,
            },
            "model_resolution": {
                "requested": model,
                "resolved": resolved_model,
                "aliases": model_aliases,
                "mode_catalog": model_catalog,
                "warning": model_warn,
                "allow_model_alias_fallback": bool(allow_model_alias_fallback),
            },
        },
    }

    def _log(msg: str, level: str = "DBG") -> None:
        if callable(log_cb):
            try:
                log_cb(msg, level)
            except Exception:
                pass

    if model_warn:
        result["errors"].append(f"model_name: {model_warn}")
        _log(model_warn, "WARN")
    if ratio_warn:
        result["errors"].append(f"aspect_ratio: {ratio_warn}")
        _log(ratio_warn, "WARN")

    def _collect_model_candidates_from_panel(panel_snapshot: dict) -> list[str]:
        """
        Trích các label model thấy trong panel để debug lỗi 'model không tồn tại'.
        """
        out: list[str] = []
        seen: set[str] = set()
        for it in (panel_snapshot or {}).get("items", []) or []:
            text = str(it.get("text") or "").strip()
            low = text.lower()
            if ("banana" in low) or ("veo" in low) or ("imagen" in low):
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        return out

    async def _record_step(
        step_key: str,
        labels: list[str],
        action_coro,
        applied_key: str,
        allow_already_selected: bool = True,
        require_selected_after: bool = False,
    ) -> bool:
        """
        Wrapper chuẩn để chạy từng step và ghi đầy đủ debug trước/sau.
        """
        row: dict = {
            "step": step_key,
            "labels": labels,
            "before": {},
            "after": {},
            "clicked": False,
            "already_selected_before": False,
            "already_selected_after": False,
            "error": "",
        }
        result["debug"]["step_traces"].append(row)

        row["before"] = await _collect_visible_clickable_snapshot(page)
        row["panel_subtree_before"] = await _collect_panel_subtree_snapshot(page)
        row["already_selected_before"] = await _is_any_label_selected(page, labels)
        _dump_debug_json(debug_root, f"{run_id}_{step_key}_panel_before.json", row["panel_subtree_before"])

        # Chụp màn hình trước step.
        pre_shot = await _dump_debug_screenshot(page, debug_root, f"{run_id}_{step_key}_before.png")
        if pre_shot:
            result["debug"]["screenshots"][f"{step_key}_before"] = pre_shot

        # Nếu trước đó đã selected rồi thì coi như pass, không bắt buộc click lại.
        if allow_already_selected and row["already_selected_before"]:
            row["clicked"] = False
            row["after"] = row["before"]
            row["already_selected_after"] = True
            result["applied"][applied_key] = True
            result["steps"].append(f"{step_key}:already_selected")
            _log(f"{step_key}: đã ở trạng thái selected trước khi click.", "DBG")
            return True

        try:
            clicked = await action_coro(row)
            row["clicked"] = bool(clicked)
        except Exception as exc:
            row["error"] = str(exc)
            result["errors"].append(f"{step_key}: {exc}")
            row["clicked"] = False

        # Sau step: snapshot + selected check
        row["after"] = await _collect_visible_clickable_snapshot(page)
        row["panel_subtree_after"] = await _collect_panel_subtree_snapshot(page)
        row["already_selected_after"] = await _is_any_label_selected(page, labels)
        _dump_debug_json(debug_root, f"{run_id}_{step_key}_panel_after.json", row["panel_subtree_after"])

        post_shot = await _dump_debug_screenshot(page, debug_root, f"{run_id}_{step_key}_after.png")
        if post_shot:
            result["debug"]["screenshots"][f"{step_key}_after"] = post_shot

        if require_selected_after:
            ok = bool(row["already_selected_after"])
        else:
            ok = bool(row["clicked"] or row["already_selected_after"])
        result["applied"][applied_key] = ok
        if ok:
            flag = "clicked" if row["clicked"] else "selected_after"
            result["steps"].append(f"{step_key}:{flag}")
        else:
            if step_key == "model_name":
                exact_reason = row.get("model_exact_fail_reason")
                if exact_reason:
                    result["errors"].append(f"{step_key}: {exact_reason}")
                    return ok
                cands = row.get("model_candidates_after") or row.get("model_candidates_before") or []
                if cands:
                    result["errors"].append(
                        f"{step_key}: không chọn được labels={labels}. Model thấy trong panel: {cands}"
                    )
                else:
                    result["errors"].append(
                        f"{step_key}: không click được và cũng không thấy trạng thái selected cho labels={labels}"
                    )
            else:
                result["errors"].append(
                    f"{step_key}: không click được và cũng không thấy trạng thái selected cho labels={labels}"
                )
        return ok

    # Snapshot toàn cục trước khi apply.
    result["debug"]["pre_snapshot"] = await _collect_visible_clickable_snapshot(page)
    global_pre = await _dump_debug_screenshot(page, debug_root, f"{run_id}_global_before.png")
    if global_pre:
        result["debug"]["screenshots"]["global_before"] = global_pre

    # 0) Bắt buộc mở panel setting từ trạng thái compact trước khi click option chi tiết.
    panel_open_row: dict = {"step": "open_settings_panel", "before": {}, "after": {}, "error": ""}
    result["debug"]["step_traces"].append(panel_open_row)
    panel_open_row["before"] = await _collect_visible_clickable_snapshot(page)
    panel_pre = await _dump_debug_screenshot(page, debug_root, f"{run_id}_open_settings_panel_before.png")
    if panel_pre:
        result["debug"]["screenshots"]["open_settings_panel_before"] = panel_pre
    try:
        panel_ok = await _ensure_generation_settings_panel_open(page, debug_row=panel_open_row)
    except Exception as exc:
        panel_ok = False
        panel_open_row["error"] = str(exc)
        result["errors"].append(f"open_settings_panel: {exc}")
    panel_open_row["after"] = await _collect_visible_clickable_snapshot(page)
    panel_post = await _dump_debug_screenshot(page, debug_root, f"{run_id}_open_settings_panel_after.png")
    if panel_post:
        result["debug"]["screenshots"]["open_settings_panel_after"] = panel_post
    if panel_ok:
        result["steps"].append("open_settings_panel:ok")
    else:
        result["errors"].append("open_settings_panel: không mở được panel setting mở rộng")

    # Nếu panel chưa mở đúng thì dừng sớm để tránh click nhầm vào popup khác.
    if not panel_ok:
        result["debug"]["post_snapshot"] = await _collect_visible_clickable_snapshot(page)
        global_post = await _dump_debug_screenshot(page, debug_root, f"{run_id}_global_after.png")
        if global_post:
            result["debug"]["screenshots"]["global_after"] = global_post
        _dump_debug_json(debug_root, f"{run_id}_flow_settings_result.json", result)
        _log(
            "Flow settings apply result: "
            f"ok={result['ok']} applied={result['applied']} errors={len(result['errors'])} "
            f"debug_dir={result['debug'].get('debug_dir')}",
            "DBG",
        )
        return result

    # 1) Tab trên cùng: Hình ảnh / Video
    #    React/Radix tab thường KHÔNG chấp nhận JS element.click(),
    #    nên cần fallback bằng mouse click vào tọa độ trung tâm nút tab.
    mode_labels = ["Video"] if top_mode_low == "video" else ["Hình ảnh", "Image"]

    async def _top_mode_action(row: dict) -> bool:
        # Thử nhiều nhịp: text click → mouse click, giống logic ratio.
        for i in range(3):
            row[f"mode_try_{i+1}"] = {"by_text": False, "by_mouse": False}

            by_text = await _click_by_visible_text(
                page,
                mode_labels,
                debug_row=row,
                exact_only=True,
                prefer_bottom=True,
                require_panel_scope=True,
            )
            row[f"mode_try_{i+1}"]["by_text"] = bool(by_text)
            # Chờ React re-render sau khi click tab
            await asyncio.sleep(0.5)
            if await _is_any_label_selected(page, mode_labels):
                row["mode_selected_on_try"] = i + 1
                return True

            # Fallback: mouse click vào tọa độ trung tâm nút tab
            by_mouse = await _click_tab_label_by_mouse_center(
                page,
                mode_labels,
                debug_row=row,
                require_panel_scope=True,
            )
            row[f"mode_try_{i+1}"]["by_mouse"] = bool(by_mouse)
            await asyncio.sleep(0.5)
            if await _is_any_label_selected(page, mode_labels):
                row["mode_selected_on_try"] = i + 1
                return True
        return False

    await _record_step(
        step_key="top_mode",
        labels=mode_labels,
        action_coro=_top_mode_action,
        applied_key="top_mode",
        require_selected_after=True,
    )

    # Sau khi switch mode, panel có thể re-render → chờ UI ổn định
    await asyncio.sleep(0.5)

    # 2) Tab phụ: Khung hình / Thành phần (nếu truyền)
    #    Cùng cơ chế: JS click thường fail trên React tab, cần mouse fallback.
    if secondary_low:
        second_labels_map = {
            "khung hình": ["Khung hình", "Frame"],
            "frame": ["Khung hình", "Frame"],
            "thành phần": ["Thành phần", "Ingredients", "Components"],
            "component": ["Thành phần", "Ingredients", "Components"],
            "components": ["Thành phần", "Ingredients", "Components"],
        }
        secondary_labels = second_labels_map.get(secondary_low, [secondary_mode])

        async def _secondary_mode_action(row: dict) -> bool:
            for i in range(3):
                row[f"secondary_try_{i+1}"] = {"by_text": False, "by_mouse": False}

                by_text = await _click_by_visible_text(
                    page,
                    secondary_labels,
                    debug_row=row,
                    exact_only=True,
                    prefer_bottom=True,
                    require_panel_scope=True,
                )
                row[f"secondary_try_{i+1}"]["by_text"] = bool(by_text)
                await asyncio.sleep(0.4)
                if await _is_any_label_selected(page, secondary_labels):
                    row["secondary_selected_on_try"] = i + 1
                    return True

                by_mouse = await _click_tab_label_by_mouse_center(
                    page,
                    secondary_labels,
                    debug_row=row,
                    require_panel_scope=True,
                )
                row[f"secondary_try_{i+1}"]["by_mouse"] = bool(by_mouse)
                await asyncio.sleep(0.4)
                if await _is_any_label_selected(page, secondary_labels):
                    row["secondary_selected_on_try"] = i + 1
                    return True
            return False

        await _record_step(
            step_key="secondary_mode",
            labels=secondary_labels,
            action_coro=_secondary_mode_action,
            applied_key="secondary_mode",
            require_selected_after=True,
        )

    # 3) Ratio (vd: 9:16 hoặc 16:9)
    if resolved_ratio:
        async def _ratio_action(row: dict) -> bool:
            # Thử nhiều nhịp để tránh case click JS không đổi selected state.
            for i in range(3):
                row[f"ratio_try_{i+1}"] = {"by_text_clicked": False, "by_mouse_clicked": False}

                by_text = await _click_by_visible_text(
                    page,
                    ratio_labels,
                    debug_row=row,
                    exact_only=True,
                    prefer_bottom=True,
                    require_panel_scope=True,
                )
                row[f"ratio_try_{i+1}"]["by_text_clicked"] = bool(by_text)
                await asyncio.sleep(0.2)
                if await _is_any_label_selected(page, ratio_labels):
                    row["ratio_selected_on_try"] = i + 1
                    return True

                by_mouse = await _click_tab_label_by_mouse_center(
                    page,
                    ratio_labels,
                    debug_row=row,
                    require_panel_scope=True,
                )
                row[f"ratio_try_{i+1}"]["by_mouse_clicked"] = bool(by_mouse)
                await asyncio.sleep(0.25)
                if await _is_any_label_selected(page, ratio_labels):
                    row["ratio_selected_on_try"] = i + 1
                    return True
            return False

        await _record_step(
            step_key="aspect_ratio",
            labels=ratio_labels,
            action_coro=_ratio_action,
            applied_key="aspect_ratio",
            require_selected_after=True,
        )

    # 4) Multiplier (vd: x4)
    if multi:
        multi_labels = [multi.lower(), multi.upper(), multi]
        await _record_step(
            step_key="multiplier",
            labels=multi_labels,
            action_coro=lambda row: _click_by_visible_text(
                page,
                multi_labels,
                debug_row=row,
                exact_only=True,
                prefer_bottom=True,
                require_panel_scope=True,
            ),
            applied_key="multiplier",
            require_selected_after=True,
        )

    # 5) Model dropdown (vd: Nano Banana Pro / Veo 3.1 - Fast)
    if resolved_model:
        model_labels = model_aliases or [resolved_model]

        async def _model_action(row: dict) -> bool:
            target_model = resolved_model
            alias_fallbacks = [x for x in model_labels if _normalize_ui_text(x) != _normalize_ui_text(target_model)]

            # 1) Nếu model hiện tại đã đúng target thì pass.
            current_before = await _get_current_model_label(page)
            row["current_model_before"] = current_before
            if _ui_text_token_match(current_before, target_model):
                row["model_exact_match_before"] = current_before
                return True

            # 2) Thu thập snapshot panel để debug.
            panel_before = await _collect_panel_subtree_snapshot(page)
            row["model_candidates_before"] = _collect_model_candidates_from_panel(panel_before)

            # 3) Mở dropdown và chọn EXACT model trước.
            opened = await _open_model_dropdown_if_needed(page, debug_row=row)
            row["dropdown_opened"] = bool(opened)
            if not opened:
                panel_after = await _collect_panel_subtree_snapshot(page)
                row["model_candidates_after"] = _collect_model_candidates_from_panel(panel_after)
                return False
            row["model_options_after_open"] = await _collect_visible_model_options(page)

            # Dùng hàm chuyên dụng cho model dropdown (dropdown bung ngoài panel scope)
            exact_ok = await _click_model_menuitem_by_text(
                page,
                [target_model],
                debug_row=row,
            )
            # Fallback: thử click bằng hàm chung nhưng KHÔNG giới hạn panel scope
            if not exact_ok:
                exact_ok = await _click_by_visible_text(
                    page,
                    [target_model],
                    debug_row=row,
                    exact_only=True,
                    prefer_bottom=True,
                    require_panel_scope=False,
                )
            row["model_exact_click_ok"] = bool(exact_ok)
            # Chờ dropdown đóng và DOM update label mới
            await asyncio.sleep(0.5)
            # Retry check vài lần vì DOM có thể cần thời gian update
            for _check_i in range(3):
                current_after_exact = await _get_current_model_label(page)
                row["current_model_after_exact"] = current_after_exact
                if _ui_text_token_match(current_after_exact, target_model):
                    return True
                await asyncio.sleep(0.3)

            # 4) Fallback alias (chỉ khi exact không thành công) + ghi warning rõ.
            if not allow_model_alias_fallback:
                row["model_exact_required"] = True
                row["model_exact_fail_reason"] = (
                    f"Không chọn được exact '{target_model}'. "
                    f"Model options đang thấy: {row.get('model_options_after_open') or []}"
                )
                return False
            for alias in alias_fallbacks:
                # Dùng hàm chuyên dụng cho dropdown model (nới rộng scope)
                alias_ok = await _click_model_menuitem_by_text(
                    page,
                    [alias],
                    debug_row=row,
                )
                if not alias_ok:
                    alias_ok = await _click_by_visible_text(
                        page,
                        [alias],
                        debug_row=row,
                        exact_only=True,
                        prefer_bottom=True,
                        require_panel_scope=False,
                    )
                await asyncio.sleep(0.2)
                current_after_alias = await _get_current_model_label(page)
                row.setdefault("model_alias_attempts", []).append(
                    {
                        "alias": alias,
                        "clicked": bool(alias_ok),
                        "current_after": current_after_alias,
                    }
                )
                if _ui_text_token_match(current_after_alias, alias):
                    row["model_alias_used"] = alias
                    row["model_alias_warning"] = (
                        f"Không chọn được exact '{target_model}', đang dùng alias '{alias}'."
                    )
                    result["errors"].append(f"model_name: {row['model_alias_warning']}")
                    _log(row["model_alias_warning"], "WARN")
                    return True

            panel_after = await _collect_panel_subtree_snapshot(page)
            row["model_candidates_after"] = _collect_model_candidates_from_panel(panel_after)
            return False

        await _record_step(
            step_key="model_name",
            labels=model_labels,
            action_coro=_model_action,
            applied_key="model_name",
        )

    # Snapshot toàn cục sau khi apply.
    result["debug"]["post_snapshot"] = await _collect_visible_clickable_snapshot(page)
    global_post = await _dump_debug_screenshot(page, debug_root, f"{run_id}_global_after.png")
    if global_post:
        result["debug"]["screenshots"]["global_after"] = global_post

    # Tiêu chí pass cơ bản:
    # - ratio + multiplier phải OK
    # - model_name nếu có truyền thì phải OK
    need_model = bool(resolved_model)
    ratio_ok = bool(result["applied"].get("aspect_ratio"))
    mult_ok = bool(result["applied"].get("multiplier"))
    model_ok = bool(result["applied"].get("model_name")) if need_model else True
    result["ok"] = ratio_ok and mult_ok and model_ok

    # Dump file debug tổng hợp để đọc offline.
    _dump_debug_json(debug_root, f"{run_id}_flow_settings_result.json", result)
    _dump_debug_json(
        debug_root,
        f"{run_id}_flow_settings_selected_hints.json",
        {
            "pre_selected_hints": result["debug"].get("pre_snapshot", {}).get("selected_like_texts", []),
            "post_selected_hints": result["debug"].get("post_snapshot", {}).get("selected_like_texts", []),
        },
    )

    _log(
        "Flow settings apply result: "
        f"ok={result['ok']} applied={result['applied']} errors={len(result['errors'])} "
        f"debug_dir={result['debug'].get('debug_dir')}",
        "DBG",
    )

    # ── Dismiss dropdown / popup còn mở sau khi click settings ───────────────
    # Radix UI dropdown đôi khi vẫn còn hiển thị sau khi chọn model/ratio.
    # Nếu không đóng → dropdown chặn click vào ô nhập prompt.
    try:
        # Bước 1: Escape để đóng popup đang mở
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.15)

        # Bước 2: Click vào vùng trống trên đầu trang để dismiss thêm
        vp = page.viewport_size or {}
        w = int(vp.get("width", 1440))
        await page.mouse.click(w // 2, 30)
        await asyncio.sleep(0.2)

        # Bước 3: Nếu vẫn còn Radix popper, bấm Escape lần nữa
        still_open = await page.evaluate(
            "() => !!document.querySelector('[data-radix-popper-content-wrapper]')"
        )
        if still_open:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.15)
    except Exception:
        pass  # Không để lỗi dismiss crash flow chính
    # ─────────────────────────────────────────────────────────────────────────

    return result
