from __future__ import annotations

import asyncio
import os
import random
from typing import Iterable


class UnusualActivityDetectedError(RuntimeError):
    """
    Bị UI chặn "unusual activity / tạo quá nhanh".

    Dùng cho luồng failover: ném lỗi lên tầng trên để đổi worker Chrome.
    """


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return int(default)


# Bật/tắt lớp humanize cho main_runner.
FLOW_MAIN_HUMANIZE_ENABLED = _env_bool("FLOW_MAIN_HUMANIZE_ENABLED", True)
FLOW_MAIN_JITTER_MIN = max(0.3, _env_float("FLOW_MAIN_JITTER_MIN", 0.85))
FLOW_MAIN_JITTER_MAX = max(FLOW_MAIN_JITTER_MIN, _env_float("FLOW_MAIN_JITTER_MAX", 1.45))
FLOW_MAIN_SOFT_PAUSE_PROB = min(0.8, max(0.0, _env_float("FLOW_MAIN_SOFT_PAUSE_PROB", 0.15)))
FLOW_MAIN_SOFT_PAUSE_MIN_SEC = max(0.0, _env_float("FLOW_MAIN_SOFT_PAUSE_MIN_SEC", 2.0))
FLOW_MAIN_SOFT_PAUSE_MAX_SEC = max(
    FLOW_MAIN_SOFT_PAUSE_MIN_SEC,
    _env_float("FLOW_MAIN_SOFT_PAUSE_MAX_SEC", 5.0),
)
FLOW_MAIN_UNUSUAL_COOLDOWN_SEC = max(30, _env_int("FLOW_MAIN_UNUSUAL_COOLDOWN_SEC", 180))

_rng = random.Random(os.environ.get("FLOW_MAIN_HUMANIZE_SEED", "").strip() or None)


def _jitter(base_sec: float, floor: float = 0.2) -> float:
    base = max(float(floor), float(base_sec))
    if not FLOW_MAIN_HUMANIZE_ENABLED:
        return base
    d = _rng.uniform(base * FLOW_MAIN_JITTER_MIN, base * FLOW_MAIN_JITTER_MAX)
    if _rng.random() < FLOW_MAIN_SOFT_PAUSE_PROB:
        d += _rng.uniform(FLOW_MAIN_SOFT_PAUSE_MIN_SEC, FLOW_MAIN_SOFT_PAUSE_MAX_SEC)
    return max(float(floor), d)


async def sleep_humanized(base_sec: float, floor: float = 0.2) -> None:
    """
    Delay có jitter để tránh nhịp thao tác đều như bot.
    """
    await asyncio.sleep(_jitter(base_sec, floor=floor))


async def has_unusual_activity_ui_error(page) -> bool:
    """
    Quét text UI để phát hiện cảnh báo "unusual activity".

    Request: đọc innerText của trang hiện tại.
    Response: True nếu có pattern bất thường, ngược lại False.
    """
    patterns: Iterable[str] = (
        "unusual activity",
        "we noticed some unusual activity",
        "hoạt động bất thường",
        "hoat dong bat thuong",
        # Rate limit kiểu "tạo quá nhanh" trên Flow.
        "yêu cầu tạo quá nhanh",
        "yeu cau tao qua nhanh",
        "ban dang yeu cau tao qua nhanh",
        "too many requests",
        "requesting too fast",
        "you are requesting too fast",
        "try again later",
        "thử lại sau",
        "vui lòng đợi một chút rồi thử lại",
        "vui long doi mot chut roi thu lai",
    )
    try:
        text = await page.evaluate(
            "() => (document && document.body && document.body.innerText) ? document.body.innerText : ''"
        )
    except Exception:
        return False
    low = str(text or "").lower()
    return any(p in low for p in patterns)


async def has_policy_violation_ui_error(page) -> bool:
    """
    Quét text UI để phát hiện prompt bị chặn vì policy/safety.

    Trả về:
    - True: có dấu hiệu bị chặn chính sách.
    - False: không phát hiện.
    """
    patterns: Iterable[str] = (
        "violates",
        "violate",
        "policy",
        "content policy",
        "safety policy",
        "not allowed",
        "not supported",
        "unsafe content",
        "can't generate",
        "cannot generate",
        "we can't generate",
        "goes against",
        "community guidelines",
        "chính sách",
        "vi phạm",
        "không được phép",
        "khong duoc phep",
        "không thể tạo",
        "khong the tao",
        "nội dung không an toàn",
        "noi dung khong an toan",
        "không phù hợp",
        "khong phu hop",
    )
    try:
        text = await page.evaluate(
            "() => (document && document.body && document.body.innerText) ? document.body.innerText : ''"
        )
    except Exception:
        return False
    low = str(text or "").lower()
    return any(p in low for p in patterns)


async def handle_unusual_activity_with_cooldown(
    page,
    stage_label: str = "",
    raise_on_detected: bool = False,
) -> bool:
    """
    Nếu UI báo unusual activity thì cooldown + reload nhẹ để giảm khả năng bị khóa sâu hơn.

    Return:
    - True: có phát hiện unusual và đã xử lý cooldown.
    - False: không phát hiện unusual.
    """
    unusual = await has_unusual_activity_ui_error(page)
    if not unusual:
        return False

    tag = f"[{stage_label}] " if stage_label else ""
    if raise_on_detected:
        msg = (
            f"Phát hiện unusual activity ở stage '{stage_label or 'unknown'}'. "
            "Dừng worker hiện tại để failover sang worker khác."
        )
        print(f"[WARN] {tag}{msg}")
        raise UnusualActivityDetectedError(msg)

    cool = _jitter(FLOW_MAIN_UNUSUAL_COOLDOWN_SEC, floor=30)
    print(
        f"[WARN] {tag}Phát hiện cảnh báo giới hạn/unusual (vd: tạo quá nhanh). "
        f"Cooldown {int(cool)}s rồi reload nhẹ."
    )
    await asyncio.sleep(cool)

    try:
        await page.reload(wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(_jitter(2.0, floor=0.8))
    except Exception as e:
        print(f"[WARN] {tag}Reload sau cooldown gặp lỗi: {e}")
    return True
