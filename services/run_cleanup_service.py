from __future__ import annotations

import os
from pathlib import Path


# Danh sách extension media cần dọn trước mỗi lần chạy để tránh nhầm output cũ/mới.
_CLEAN_EXTENSIONS = {".mp4", ".png", ".jpg", ".jpeg"}


def _collect_media_files_in_dir(dir_path: str) -> list[str]:
    """
    Thu thập file media trong 1 thư mục (không quét đệ quy).

    Request:
    - Nhận đường dẫn thư mục cần quét.

    Response:
    - Trả về danh sách absolute path của file có extension thuộc nhóm cần dọn.
    """
    out: list[str] = []
    p = Path(dir_path)
    if not p.exists() or not p.is_dir():
        return out

    for item in p.iterdir():
        if not item.is_file():
            continue
        if item.suffix.lower() in _CLEAN_EXTENSIONS:
            out.append(str(item))
    return out


def cleanup_scenario_media_files(scenario_dirs: list[str]) -> dict[str, int]:
    """
    Dọn media files trước khi chạy để tránh lẫn dữ liệu cũ.

    Phạm vi dọn cho mỗi scenario:
    1) Thư mục gốc kịch bản (ví dụ: scenarios/kich_ban_A)
    2) Thư mục output của kịch bản (ví dụ: scenarios/kich_ban_A/output)

    Lưu ý:
    - Chỉ xóa file .mp4/.png/.jpg/.jpeg.
    - Không xóa file txt/json/cấu hình.
    """
    deleted = 0
    failed = 0

    for scenario_dir in scenario_dirs:
        targets = [
            str(scenario_dir),
            os.path.join(str(scenario_dir), "output"),
        ]
        for tdir in targets:
            for fpath in _collect_media_files_in_dir(tdir):
                try:
                    os.remove(fpath)
                    deleted += 1
                except Exception:
                    failed += 1

    return {"deleted": deleted, "failed": failed}
