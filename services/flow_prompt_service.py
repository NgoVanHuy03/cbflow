"""
Service nhập và gửi prompt trên Google Flow editor.

Mục tiêu:
- Tách riêng logic UI tương tác prompt khỏi dreamina.py để dễ maintain.
- Có thể gọi độc lập sau khi apply_flow_generation_settings_panel() xong.
- Hỗ trợ Google Flow (contenteditable) — KHÔNG dùng cho Dreamina cũ.

Luồng gọi thông thường:
    await find_and_focus_flow_prompt(page)
    await type_flow_prompt(page, text)
    await send_flow_prompt(page)
"""

from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path

# ── Đường dẫn động ─────────────────────────────────────────────────────────
_SERVICE_DIR = Path(__file__).resolve().parent


# ── Selector tìm ô nhập prompt (theo thứ tự ưu tiên) ─────────────────────
# Google Flow dùng div contenteditable, không phải <textarea> thông thường.
FLOW_PROMPT_SELECTORS = [
    "div[contenteditable='true'][data-placeholder]",
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true']",
    "[role='textbox']",
    "textarea",
]

# ── Selector tìm nút Tạo / Generate (fallback khi Enter không hoạt động) ──
FLOW_SEND_SELECTORS = [
    "button[data-testid='generate-btn']",
    "button[aria-label*='Generate']",
    "button[aria-label*='Tạo']",
    "button[aria-label*='generate']",
    "button[type='submit']",
]


def _log(msg: str, level: str = "DBG") -> None:
    """Log đơn giản ra stdout, không cần import dreamina.py."""
    icon = {
        "OK":   "✅",
        "WARN": "⚠️ ",
        "ERR":  "❌",
        "DBG":  "   ",
    }.get(level, "   ")
    print(f"  {icon} [flow_prompt] {msg}", flush=True)


# ════════════════════════════════════════════════════════════════════════════
#  TÌM Ô NHẬP PROMPT
# ════════════════════════════════════════════════════════════════════════════

async def _find_flow_prompt_element(page):
    """
    Tìm element ô nhập prompt bằng danh sách selector ưu tiên.
    Trả về element nếu tìm thấy và visible, ngược lại trả None.
    """
    for sel in FLOW_PROMPT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.count() > 0 and await el.is_visible(timeout=800):
                return el
        except Exception:
            pass
    return None


async def find_and_focus_flow_prompt(page, quick: bool = False):
    """
    Tìm và focus vào ô nhập prompt của Google Flow.

    Chiến lược:
    1. Thử selector → click element
    2. Fallback pixel click vào vùng giữa-dưới trang (vị trí thường của ô nhập)
    3. Kiểm tra activeElement có phải input/contenteditable không

    Trả về element nếu focus thành công qua selector, None nếu dùng pixel click.
    """
    # ── Thử selector ──
    click_wait = 0.12 if quick else 0.3
    pixel_wait = 0.16 if quick else 0.4

    el = await _find_flow_prompt_element(page)
    if el:
        try:
            await el.click()
            await asyncio.sleep(click_wait)
            _log("Focus vào ô nhập (selector)", "OK")
            return el
        except Exception:
            pass

    # ── Fallback pixel click ──
    vp = page.viewport_size or {}
    w = int(vp.get("width", 1440))
    h = int(vp.get("height", 900))

    # Thử nhiều tọa độ vùng prompt (Google Flow ô nhập thường ở dưới cùng)
    pixel_positions = [
        (w * 0.50, h - 130),
        (w * 0.50, h - 110),
        (w * 0.50, h - 155),
        (w * 0.45, h - 130),
        (w * 0.55, h - 130),
    ]

    for px, py in pixel_positions:
        try:
            await page.mouse.click(px, py)
            await asyncio.sleep(pixel_wait)

            # Kiểm tra element đang focus có phải ô nhập không
            tag      = await page.evaluate("document.activeElement?.tagName || ''")
            editable = await page.evaluate("document.activeElement?.contentEditable || ''")
            role     = await page.evaluate("document.activeElement?.getAttribute('role') || ''")

            is_input = (
                tag.lower() in ["textarea", "input"]
                or editable == "true"
                or role == "textbox"
            )
            if is_input:
                _log(f"Focus vào ô nhập (pixel {int(px)},{int(py)})", "OK")
                return None  # đã focus, dùng keyboard từ đây
        except Exception:
            pass

    _log("Không tìm được ô nhập prompt", "WARN")
    return None


# ════════════════════════════════════════════════════════════════════════════
#  NHẬP PROMPT
# ════════════════════════════════════════════════════════════════════════════

async def type_flow_prompt(page, text: str, fast: bool = False, skip_focus: bool = False) -> None:
    """
    Click ô nhập → xóa nội dung cũ → paste text mới vào ô prompt.

    Bản mới KHÔNG dùng clipboard hệ thống để tránh ảnh hưởng copy/paste của người dùng.
    Thay vào đó:
    - Set text trực tiếp vào input/contenteditable.
    - Bắn sự kiện input/change để UI Flow nhận prompt mới.
    """
    # Focus vào ô nhập trước
    if not skip_focus:
        await find_and_focus_flow_prompt(page, quick=fast)
    await asyncio.sleep(0.03 if fast else 0.1)

    # Hàm kiểm tra nhanh ô prompt hiện có bao nhiêu ký tự (để tránh trường hợp Enter khi ô rỗng).
    async def _read_prompt_len() -> int:
        try:
            val = await page.evaluate(
                """
                () => {
                  const el = document.activeElement;
                  if (!el) return '';
                  const tag = String(el.tagName || '').toLowerCase();
                  if (tag === 'textarea' || tag === 'input') return String(el.value || '');
                  if (el.isContentEditable) return String(el.innerText || el.textContent || '');
                  return '';
                }
                """
            )
            return len(str(val or "").strip())
        except Exception:
            return 0

    # Cách chính: nhập bằng keyboard.insert_text (không dùng clipboard, ổn định với contenteditable).
    inserted_ok = False
    try:
        select_all = "Meta+a" if platform.system() == "Darwin" else "Control+a"
        await page.keyboard.press(select_all)
        await asyncio.sleep(0.01 if fast else 0.05)
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.01 if fast else 0.08)
        await page.keyboard.insert_text(text)
        await asyncio.sleep(0.03 if fast else 0.12)
        inserted_ok = (await _read_prompt_len()) > 0
    except Exception:
        inserted_ok = False

    # Fallback 1: set trực tiếp value/textContent + dispatch input/change.
    if not inserted_ok:
        inserted_ok = await page.evaluate(
            """
            (t) => {
              const el = document.activeElement;
              if (!el) return false;
              const tag = String(el.tagName || '').toLowerCase();
              const isInput = (tag === 'textarea') || (tag === 'input');
              const isEditable = !!el.isContentEditable;

              if (isInput) {
                el.focus();
                el.value = String(t ?? '');
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              }
              if (isEditable) {
                el.focus();
                el.textContent = String(t ?? '');
                el.dispatchEvent(new InputEvent('input', {
                  bubbles: true,
                  inputType: 'insertText',
                  data: String(t ?? ''),
                }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              }
              return false;
            }
            """,
            text,
        )
        await asyncio.sleep(0.03 if fast else 0.1)
        inserted_ok = inserted_ok and (await _read_prompt_len()) > 0

    # Fallback 2: thử fill qua locator nếu vẫn chưa có text.
    if not inserted_ok:
        el = await _find_flow_prompt_element(page)
        if el:
            try:
                await el.fill(text)
                await asyncio.sleep(0.03 if fast else 0.1)
                inserted_ok = (await _read_prompt_len()) > 0
            except Exception:
                inserted_ok = False

    if inserted_ok:
        _log(f"Đã nhập prompt trực tiếp ({len(text)} ký tự)", "OK")
    else:
        _log("Không xác nhận được nội dung prompt sau khi nhập", "WARN")


# ════════════════════════════════════════════════════════════════════════════
#  GỬI PROMPT (nhấn Generate)
# ════════════════════════════════════════════════════════════════════════════

async def send_flow_prompt(
    page,
    refocus_before_send: bool = True,
    fast: bool = False,
) -> bool:
    """
    Gửi prompt đã nhập trên Google Flow.

    Chiến lược:
    1. Re-focus vào ô nhập để đảm bảo keyboard event đến đúng nơi.
    2. Nhấn Enter để submit.
    3. Fallback: click nút Generate qua selector nếu Enter không hoạt động.

    Trả về True nếu đã gửi thành công (đã nhấn Enter hoặc click nút).
    """
    await asyncio.sleep(0.02 if fast else 0.08)

    # ── Bước 1: Re-focus ô nhập ──
    if refocus_before_send:
        el = await _find_flow_prompt_element(page)
        if el:
            try:
                await el.click()
            except Exception:
                pass
        else:
            # Pixel click fallback vào giữa-dưới trang
            vp = page.viewport_size or {}
            w = int(vp.get("width", 1440))
            h = int(vp.get("height", 900))
            await page.mouse.click(w * 0.5, h - 130)

        await asyncio.sleep(0.02 if fast else 0.08)

    # ── Bước 2: Kiểm tra ô prompt có text chưa (tránh Enter khi rỗng gây toast lỗi) ──
    try:
        prompt_len = await page.evaluate(
            """
            () => {
              const el = document.activeElement;
              if (!el) return 0;
              const tag = String(el.tagName || '').toLowerCase();
              let txt = '';
              if (tag === 'textarea' || tag === 'input') {
                txt = String(el.value || '');
              } else if (el.isContentEditable) {
                txt = String(el.innerText || el.textContent || '');
              }
              return String(txt).trim().length;
            }
            """
        )
    except Exception:
        prompt_len = 0

    if int(prompt_len or 0) <= 0:
        _log("Ô prompt đang rỗng, bỏ qua Enter để tránh lỗi submit rỗng", "WARN")
        return False

    # ── Bước 3: Nhấn Enter ──
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.12 if fast else 0.5)
    _log("Đã nhấn Enter gửi prompt", "OK")
    return True


# ════════════════════════════════════════════════════════════════════════════
#  TIỆN ÍCH: Click nút Generate trực tiếp (không qua Enter)
# ════════════════════════════════════════════════════════════════════════════

async def click_flow_generate_button(page) -> bool:
    """
    Click thẳng vào nút Generate/Tạo thay vì nhấn Enter.
    Dùng khi Enter không trigger submit (xảy ra ở một số version UI).
    """
    for sel in FLOW_SEND_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible(timeout=600):
                await btn.click()
                await asyncio.sleep(0.5)
                _log(f"Đã click nút Generate ({sel})", "OK")
                return True
        except Exception:
            pass

    # JS fallback: tìm nút có text "Tạo" / "Generate" gần ô nhập
    try:
        clicked = await page.evaluate(
            """
            () => {
              const norm = (s) => String(s || '').toLowerCase().trim();
              const keywords = ['tạo', 'generate', 'create', 'send'];
              const nodes = Array.from(document.querySelectorAll('button'));
              for (const btn of nodes) {
                const txt = norm(btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '');
                if (keywords.some(k => txt === k || txt.startsWith(k + ' '))) {
                  btn.click();
                  return true;
                }
              }
              return false;
            }
            """
        )
        if clicked:
            await asyncio.sleep(0.5)
            _log("Đã click nút Generate (JS fallback)", "OK")
            return True
    except Exception:
        pass

    _log("Không tìm thấy nút Generate", "WARN")
    return False
