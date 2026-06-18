"""
utils/screen_utils.py
─────────────────────
Tiện ích phát hiện kích thước màn hình thực của máy.
Dùng chung cho parallel_runner.py và dreamina.py khi khởi động Chrome.

Thứ tự thử:
  1. AppleScript (macOS native, không cần cài gì)
  2. Quartz     (macOS, nếu pyobjc-framework-Quartz đã cài)
  3. tkinter    (cross-platform, Python built-in)
  4. Fallback   → 1920×1080
"""

from __future__ import annotations

import subprocess
import platform


def get_screen_size() -> tuple[int, int]:
    """
    Trả về (width, height) của màn hình chính tính bằng pixel.

    Ví dụ sử dụng:
        from utils.screen_utils import get_screen_size, SCREEN_W, SCREEN_H
        print(get_screen_size())   # (1920, 1080)
    """

    # ── Cách 1: AppleScript — macOS native, không cần cài gì ─────────────────
    if platform.system() == "Darwin":
        try:
            script = 'tell application "Finder" to get bounds of window of desktop'
            out = subprocess.check_output(
                ["osascript", "-e", script], timeout=5
            ).decode().strip()
            # Output dạng: "0, 0, 1920, 1080"
            parts = [int(x.strip()) for x in out.split(",")]
            if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
                return parts[2], parts[3]
        except Exception:
            pass

        # ── Cách 2: Quartz (nếu pyobjc-framework-Quartz đã cài) ──────────────
        try:
            import Quartz  # type: ignore
            mode = Quartz.CGDisplayCopyDisplayMode(Quartz.CGMainDisplayID())
            w = int(Quartz.CGDisplayModeGetWidth(mode))
            h = int(Quartz.CGDisplayModeGetHeight(mode))
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass

    # ── Cách 3: tkinter — cross-platform ─────────────────────────────────────
    try:
        import tkinter as _tk
        _root = _tk.Tk()
        _root.withdraw()   # ẩn cửa sổ tkinter, không hiện lên màn hình
        w = _root.winfo_screenwidth()
        h = _root.winfo_screenheight()
        _root.destroy()
        if w > 0 and h > 0:
            return int(w), int(h)
    except Exception:
        pass

    # ── Fallback an toàn ──────────────────────────────────────────────────────
    return 1920, 1080


# Cache kích thước màn hình — detect 1 lần khi import module,
# tránh gọi subprocess mỗi lần cần dùng.
SCREEN_W, SCREEN_H = get_screen_size()
