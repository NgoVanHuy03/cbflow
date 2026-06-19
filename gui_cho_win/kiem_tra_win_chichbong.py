#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CHẨN ĐOÁN Windows — xem ChichBong (Imagen4) lưu license + hardware ID ở đâu.

Chạy trên máy WINDOWS đã cài + kích hoạt app ChichBong:
    python kiem_tra_win_chichbong.py

Rồi COPY toàn bộ kết quả gửi lại để mình viết phần tự đọc license/fingerprint
cho đúng máy Windows (không hardcode Mac).

Script chỉ ĐỌC, không sửa gì trong Registry.
"""

import sys
import subprocess


def _print_system_uuid():
    print("\n========== SYSTEM UUID (mainboard_uuid) ==========")
    try:
        out = subprocess.run(
            ["wmic", "csproduct", "get", "uuid"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        print(out or "(trống)")
    except Exception as e:
        # Windows mới có thể bỏ wmic → thử PowerShell
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
                capture_output=True, text=True, timeout=20,
            ).stdout.strip()
            print(out or "(trống)")
        except Exception as e2:
            print(f"Không lấy được UUID: {e} | {e2}")


def _walk(winreg, root, path, depth=0, max_depth=6):
    """In đệ quy mọi value + subkey dưới 1 nhánh registry."""
    indent = "  " * depth
    try:
        key = winreg.OpenKey(root, path)
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"{indent}[lỗi mở {path}: {e}]")
        return False

    # In các value
    i = 0
    while True:
        try:
            name, value, _ = winreg.EnumValue(key, i)
        except OSError:
            break
        # Cắt bớt value quá dài cho gọn
        sval = str(value)
        if len(sval) > 120:
            sval = sval[:120] + "...(cắt bớt)"
        print(f"{indent}  • {name} = {sval}")
        i += 1
    if i == 0:
        print(f"{indent}  (không có value trực tiếp)")

    # Đệ quy subkey
    if depth < max_depth:
        j = 0
        while True:
            try:
                sub = winreg.EnumKey(key, j)
            except OSError:
                break
            print(f"{indent}  └─ [{sub}]")
            _walk(winreg, root, path + "\\" + sub, depth + 1, max_depth)
            j += 1
    winreg.CloseKey(key)
    return True


def main():
    if not sys.platform.startswith("win"):
        print("⚠️  Script này để chạy trên WINDOWS. Hệ điều hành hiện tại:", sys.platform)
        print("   Hãy copy file này sang máy Windows rồi chạy: python kiem_tra_win_chichbong.py")
        return

    import winreg

    _print_system_uuid()

    print("\n========== REGISTRY: dò các nhánh ChichBong/Imagen4/ElevenLabs ==========")
    # Các org/app QSettings có thể dùng (theo source: Imagen4 / Imagen4 Client,
    # và 'old app' ElevenLabs).
    candidates = [
        r"Software\Imagen4",
        r"Software\Imagen4\Imagen4 Client",
        r"Software\ElevenLabs",
        r"Software\ElevenLabs\ElevenLabs TTS Client",
        r"Software\chichbong",
    ]
    roots = [(winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")]
    found_any = False
    for root, rname in roots:
        for path in candidates:
            print(f"\n--- {rname}\\{path} ---")
            ok = _walk(winreg, root, path)
            if ok:
                found_any = True
            else:
                print("  (không tồn tại)")

    if not found_any:
        print("\n⚠️  Không thấy nhánh nào. Có thể app dùng org/app khác.")
        print("   Hãy mở 'regedit' → tìm (Ctrl+F) từ khóa: license  hoặc  Imagen")
        print("   rồi chụp lại nhánh chứa 'license/key'.")

    print("\n========== XONG — copy toàn bộ output ở trên gửi lại ==========")


if __name__ == "__main__":
    main()
