from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models.worker_config import WorkerConfig
from services.prompt_service import (
    SCENARIO_IMAGE_FILE,
    parse_character_file,
    parse_image_prompts_file,
)

# LƯU Ý: các import của engine Flow (Playwright) — FlowSettings, ImageJob,
# generate_scene_images_from_job, WorkerPool, flow_settings_service... — được
# nạp LAZY bên trong hàm chạy Flow để engine imagen4 (và bản Windows) không
# cần cài Playwright. Xem _generate_scene_images_with_flow_for_prompt_file().

# Tên file prompt nhân vật lưu local trong mỗi folder kịch bản.
SCENARIO_CHARACTER_PROMPTS_FILE = "character_prompts.txt"
# Tên folder con (trong output) chứa ảnh nhân vật.
CHARACTERS_SUBDIR = "characters"


@dataclass
class SheetFlowConfig:
    """
    Cấu hình cho pipeline Sheet -> Flow -> Drive.

    Giải thích dễ hiểu:
    - `sheet`: link hoặc ID Google Sheet chứa danh sách job.
    - `credentials`: file credentials OAuth/Service Account.
    - `token_file`: nơi lưu token OAuth sau khi login lần đầu.
    - `drive_output_parent_id`: folder Drive cha để tạo folder kết quả ảnh cho từng dòng.
    - `workspace_dir`: nơi lưu tạm scenario local.
    - `video_workers_config`: file config worker để mở Chrome profile chạy Flow.
    """

    sheet: str
    credentials: str
    token_file: str
    drive_output_parent_id: str
    workspace_dir: str = "scenarios/sheets_pipeline"
    video_workers_config: str = "config/video_workers.json"
    use_proxy: bool = False
    public_link: bool = True

    # Mapping cột trên Google Sheet
    col_prompt_folder: str = "Prompt tạo ảnh"
    col_title: str = "Tiêu đề"
    col_output_folder: str = "Folder ảnh"

    # Giới hạn vùng đọc
    row_start: int = 2
    row_end: int = 0
    max_rows: int = 2000
    range_columns: str = "A:AZ"

    # Tên file prompt cần tải từ Drive folder
    drive_prompt_filename: str = "image_prompts.txt"
    # Tên file prompt nhân vật (tùy chọn) trong cùng Drive folder.
    # Nếu folder không có file này thì bỏ qua bước tạo ảnh nhân vật.
    drive_character_prompt_filename: str = "character_prompts.txt"

    # Retry Google API
    google_retry_attempts: int = 3
    google_retry_sleep_sec: float = 5.0

    # Cấu hình scene pipeline (áp dụng cả khi chạy multi-sheet):
    # - Hết scenario_timeout_sec thì chốt ảnh hiện có, không fail vì thiếu target.
    scene_timeout_per_prompt_sec: int = 180
    scenario_timeout_sec: int = 30 * 60
    scene_min_success_images: int = 120  # mục tiêu mềm để retry, không phải điều kiện fail cứng
    scene_retry_failed_rounds: int = 2

    # ── ChichBong Imagen4 engine ──────────────────────────────────────────────
    # generator:
    #   "imagen4" → luôn dùng ChichBong Imagen4 API — mặc định
    #   "flow"    → luôn dùng Google Flow (Dreamina)
    #   "auto"    → xen kẽ: dòng lẻ dùng flow, dòng chẵn dùng imagen4;
    #               nếu 1 engine lỗi thì tự fallback sang engine kia
    generator: str = "imagen4"
    imagen4_license_key: str = ""
    # aspect_ratio cho imagen4: square | landscape | portrait
    imagen4_aspect_ratio: str = "square"
    # quality output theo API ChichBong: 1k | 2k | 4k
    imagen4_quality: str = "2k"
    # số prompt chạy đồng thời cho Imagen4 (mặc định giống Flow: 6)
    imagen4_max_in_flight: int = 6
    # bật model legacy IMAGEN_3_5
    imagen4_use_legacy_model: bool = False
    # seed cố định (0 = auto random)
    imagen4_seed: int = 0
    # model name tuỳ chọn (chỉ dùng khi không bật legacy)
    imagen4_image_model_name: str = ""

    # ── Dọn dẹp local ─────────────────────────────────────────────────────────
    # fresh_run: mỗi lần chạy là chạy MỚI — dọn ảnh local cũ trước khi generate
    # (áp dụng cả imagen4), không resume/tái dùng ảnh cũ.
    fresh_run: bool = True
    # delete_local_after_done: sau khi upload Drive + ghi sheet thành công,
    # xóa toàn bộ thư mục kịch bản local để giải phóng ổ đĩa.
    delete_local_after_done: bool = True

    # ── Song song 2 tiến trình ────────────────────────────────────────────────
    # row_stride=2, row_offset=0 → tiến trình này xử lý hàng dữ liệu thứ 1,3,5...
    # row_stride=2, row_offset=1 → tiến trình kia xử lý hàng dữ liệu thứ 2,4,6...
    # Mặc định stride=1 offset=0 → xử lý tất cả hàng (không chia)
    row_stride: int = 1
    row_offset: int = 0

    # row_ids: danh sách số hàng cụ thể cần xử lý (do coordinator phân công)
    # Rỗng = xử lý tất cả hàng thoả row_start/row_end/stride
    row_ids: list = None  # type: ignore[assignment]


def _log(msg: str) -> None:
    """Log ngắn gọn, dễ đọc để theo dõi pipeline."""
    print(f"[sheet-flow] {msg}", flush=True)


def _format_duration(seconds: float) -> str:
    """Đổi số giây thành chuỗi dễ đọc, ví dụ: 1h 02m 03s."""
    secs = int(round(max(0.0, float(seconds))))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _append_scenario_timing(workspace_dir: str, line: str) -> None:
    """
    Ghi (append) 1 dòng thời gian xử lý kịch bản vào file .txt.

    File mặc định: <workspace_dir>/thoi_gian_kich_ban.txt
    Mỗi lần xử lý xong 1 kịch bản (dù ok hay lỗi) sẽ ghi thêm 1 dòng.
    """
    try:
        out_dir = Path(workspace_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "thoi_gian_kich_ban.txt"
        with out_path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception as exc:  # không để việc ghi log làm hỏng pipeline chính
        _log(f"Cảnh báo: không ghi được file thời gian kịch bản: {exc}")


def _retry_google_call(fn, attempts: int = 3, sleep_sec: float = 1.2):
    """
    Retry nhẹ cho call Google API khi lỗi tạm thời.

    Request:
    - Gọi `fn()` (thường là `.execute()`).
    Response:
    - Trả về kết quả ngay nếu thành công.
    - Nếu fail, retry tối đa `attempts` lần rồi raise lỗi cuối cùng.
    """
    import time

    last_exc = None
    max_try = max(1, int(attempts or 1))
    delay = max(0.2, float(sleep_sec or 0.2))
    for i in range(1, max_try + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i >= max_try:
                break
            # 403 = không có quyền, retry vô ích
            if "403" in str(exc):
                break
            # Rate limit 429 → chờ lâu hơn để quota reset (60s)
            wait = 62.0 if "429" in str(exc) else delay
            _log(f"Google API lỗi, retry {i + 1}/{max_try} sau {wait:.0f}s: {exc}")
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def _extract_sheet_id(raw: str) -> str:
    """Tách spreadsheet ID từ URL hoặc nhận ID thuần."""
    text = str(raw or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", text)
    return m.group(1) if m else text


def _extract_drive_folder_id(raw: str) -> str:
    """Tách Drive folder ID từ URL hoặc nhận ID thuần."""
    text = str(raw or "").strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", text)
    if m:
        return m.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{20,}$", text):
        return text
    return ""


def _is_drive_link_or_id(value: str) -> bool:
    """
    Kiểm tra chuỗi có phải link/ID Drive folder hợp lệ không.

    Mục tiêu:
    - Chỉ chạy khi cột "Prompt tạo ảnh" thực sự có link/ID Drive.
    - Tránh coi text thường là prompt link rồi báo lỗi giả.
    """
    text = str(value or "").strip()
    if not text:
        return False
    if "drive.google.com" in text and "/folders/" in text:
        return True
    return bool(re.match(r"^[a-zA-Z0-9_-]{20,}$", text))


def _extract_imagen4_license_from_text(text: str) -> str:
    m = re.search(r'^\s*LICENSE_KEY\s*=\s*["\']([^"\']+)["\']', str(text or ""), flags=re.MULTILINE)
    return str(m.group(1) if m else "").strip()


def _candidate_imagen4_client_files() -> list[Path]:
    out: list[Path] = []
    env_path = str(os.environ.get("CHICHBONG_CLIENT_FILE", "") or "").strip()
    if env_path:
        out.append(Path(env_path))

    root = Path(__file__).resolve().parent.parent
    out.extend(
        [
            Path("/Users/may6/Downloads/chichbong/chichbongtaoanh/chichbong_api_client.py"),
            root.parent.parent / "chichbong" / "chichbongtaoanh" / "chichbong_api_client.py",
        ]
    )

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        k = str(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def _resolve_imagen4_license_key(explicit_key: str) -> str:
    key = str(explicit_key or "").strip()
    if key:
        return key

    for env_key in ("IMAGEN4_LICENSE_KEY", "CHICHBONG_LICENSE_KEY"):
        v = str(os.environ.get(env_key, "") or "").strip()
        if v:
            _log(f"[imagen4] Dùng license key từ ENV: {env_key}")
            return v

    for path in _candidate_imagen4_client_files():
        try:
            if not path.exists() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            v = _extract_imagen4_license_from_text(text)
            if v:
                _log(f"[imagen4] Auto nạp license key từ: {path}")
                return v
        except Exception:
            continue

    # Cuối cùng: thử Registry Windows / file client (cross-platform).
    try:
        from services.chichbong_imagen_service import resolve_license_from_client
        v = resolve_license_from_client()
        if v:
            _log("[imagen4] Auto nạp license key từ Registry/client ChichBong.")
            return v
    except Exception:
        pass
    return ""


def _a1_to_col_index(label: str) -> int:
    """A1 label -> 0-based index."""
    text = re.sub(r"[^A-Z]", "", str(label or "").upper())
    v = 0
    for ch in text:
        v = v * 26 + (ord(ch) - 64)
    return max(0, v - 1)


def _col_index_to_a1(idx: int) -> str:
    """0-based index -> A1 label."""
    n = int(idx) + 1
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _find_header_row(rows: list[list[str]], col_name: str) -> int:
    """Tìm dòng header có chứa tên cột cần dùng."""
    target = str(col_name or "").strip()
    for i, row in enumerate(rows):
        for c in row:
            if str(c).strip() == target:
                return i
    return 0


def _find_col(header: list[str], name: str) -> int | None:
    """Tìm index cột theo tên hiển thị."""
    target = str(name or "").strip()
    for i, c in enumerate(header):
        if str(c).strip() == target:
            return i
    return None


def _sanitize_name(text: str, fallback: str) -> str:
    """Chuẩn hóa tên file/folder an toàn."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(text or "").strip()).strip("_")
    return cleaned[:120] if cleaned else fallback


def _cleanup_local_output_images(output_dir: Path) -> int:
    """
    Dọn ảnh cũ trong output của một row trước khi generate mới.

    Mục tiêu:
    - Tránh cộng dồn ảnh từ lần chạy trước (gây upload quá nhiều file).
    - Chỉ xóa file ảnh output, không đụng prompt/config.
    """
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    deleted = 0
    if not output_dir.exists():
        return deleted
    for p in output_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        try:
            p.unlink()
            deleted += 1
        except Exception:
            # Nếu xóa lỗi thì bỏ qua, bước generate vẫn thử chạy tiếp.
            pass
    return deleted


def _list_local_output_images(output_dir: Path) -> list[Path]:
    """
    Liệt kê ảnh output hiện có theo thứ tự tên file.
    """
    if not output_dir.exists():
        return []
    out: list[Path] = []
    for p in output_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        out.append(p)
    return sorted(out, key=lambda x: x.name.lower())


def _extract_scene_idx_from_output_name(name: str) -> int:
    """
    Tách scene index từ tên file output.

    Hỗ trợ:
    - canh_001.png
    - <job>_001_*.png
    """
    text = str(name or "")
    m = re.search(r"canh_(\d{1,4})\.(png|jpe?g|webp)$", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r"_(\d{3,4})(?:_|\.)(?:img\d+)?", text, flags=re.IGNORECASE)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            return 0
    return 0


def _collect_completed_scene_indices(output_dir: Path, total_prompts: int) -> set[int]:
    """
    Gom tập scene đã có file ảnh, dùng để resume phần còn thiếu.
    """
    done: set[int] = set()
    for p in _list_local_output_images(output_dir):
        idx = _extract_scene_idx_from_output_name(p.name)
        if 1 <= idx <= total_prompts:
            done.add(idx)
    return done


def _build_zip_from_images(image_paths: list[Path], zip_path: Path) -> Path:
    """
    Nén toàn bộ ảnh cảnh thành 1 file zip duy nhất để upload nhanh hơn.
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for img in image_paths:
            if not img.exists() or not img.is_file():
                continue
            # Lưu theo basename để khi giải nén không bị path local dài.
            zf.write(img, arcname=img.name)
    return zip_path


def _build_google_services(credentials_path: Path, token_path: Path):
    """
    Tạo Google Sheets + Drive service từ credentials.

    Hỗ trợ:
    - OAuth desktop (installed/web)
    - Service Account
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials as UserCredentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Thiếu thư viện Google API. Cài: pip install google-api-python-client google-auth google-auth-oauthlib"
        ) from exc

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    raw = json.loads(credentials_path.read_text(encoding="utf-8"))
    is_oauth = isinstance(raw, dict) and ("installed" in raw or "web" in raw)

    if is_oauth:
        creds: Any = None
        if token_path.exists():
            try:
                creds = UserCredentials.from_authorized_user_file(str(token_path), scopes)
            except Exception:
                creds = None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    else:
        creds = service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=scopes
        )

    sheets_svc = build("sheets", "v4", credentials=creds)
    drive_svc = build("drive", "v3", credentials=creds)
    return sheets_svc, drive_svc


def _read_sheet_rows_with_hidden_links(
    sheets_svc: Any,
    spreadsheet_id: str,
    tab_name: str,
    columns: str,
    max_rows: int,
    retry_attempts: int = 3,
    retry_sleep_sec: float = 1.2,
) -> tuple[list[list[str]], dict[tuple[int, int], str]]:
    """
    Đọc text + hyperlink ẩn từ sheet.

    Request gửi đi:
    - values.get để lấy text hiển thị.
    - spreadsheets.get(fields=...) để lấy hyperlink thật trong ô.

    Response nhận về:
    - rows: dữ liệu text theo dòng/cột.
    - hidden_links: map (row_idx, col_idx) -> URL ẩn trong ô.
    """
    parts = str(columns or "A:AZ").upper().split(":", 1)
    c_start = _a1_to_col_index(parts[0])
    c_end = _a1_to_col_index(parts[1] if len(parts) > 1 else parts[0])
    c_end = max(c_start, c_end)
    r_end = max(2, int(max_rows or 2000))
    rng = f"'{tab_name}'!{_col_index_to_a1(c_start)}1:{_col_index_to_a1(c_end)}{r_end}"

    rows_resp = _retry_google_call(
        lambda: sheets_svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute(),
        attempts=retry_attempts,
        sleep_sec=retry_sleep_sec,
    )
    rows: list[list[str]] = rows_resp.get("values", [])

    raw_cells = _retry_google_call(
        lambda: sheets_svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[rng],
            fields=(
                "sheets/data/rowData/values/hyperlink,"
                "sheets/data/rowData/values/userEnteredValue,"
                "sheets/data/rowData/values/textFormatRuns/format/link/uri"
            ),
        )
        .execute(),
        attempts=retry_attempts,
        sleep_sec=retry_sleep_sec,
    )
    hidden_links: dict[tuple[int, int], str] = {}
    row_data = raw_cells.get("sheets", [{}])[0].get("data", [{}])[0].get("rowData", [])
    for r_i, row_item in enumerate(row_data):
        for c_i, cell in enumerate((row_item or {}).get("values", []) or []):
            hl = str((cell or {}).get("hyperlink", "")).strip()
            if hl:
                hidden_links[(r_i, c_i)] = hl
                continue
            uv = (cell or {}).get("userEnteredValue", {}) or {}
            formula = str(uv.get("formulaValue", "")).strip()
            m = re.search(r'HYPERLINK\("([^"]+)"', formula, flags=re.IGNORECASE)
            if m:
                hidden_links[(r_i, c_i)] = m.group(1).strip()
                continue
            for run in (cell or {}).get("textFormatRuns", []) or []:
                uri = str((((run or {}).get("format", {}) or {}).get("link", {}) or {}).get("uri", "")).strip()
                if uri:
                    hidden_links[(r_i, c_i)] = uri
                    break
    return rows, hidden_links


def _download_text_file_from_drive_folder(
    drive_svc: Any,
    folder_link_or_id: str,
    target_filename: str,
    retry_attempts: int = 3,
    retry_sleep_sec: float = 1.2,
) -> str:
    """
    Tải file txt từ Drive folder.

    Request gửi đi:
    1) files.list để liệt kê file trong folder.
    2) files.get_media để tải nội dung txt.

    Response nhận về:
    - Nội dung text của file target_filename.
    """
    from googleapiclient.http import MediaIoBaseDownload
    import io

    folder_id = _extract_drive_folder_id(folder_link_or_id)
    if not folder_id:
        raise ValueError(f"Không tách được folder ID từ '{folder_link_or_id}'")

    files_resp = _retry_google_call(
        lambda: drive_svc.files()
        .list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType)",
            pageSize=100,
        )
        .execute(),
        attempts=retry_attempts,
        sleep_sec=retry_sleep_sec,
    )
    files = files_resp.get("files", [])
    target = None
    for f in files:
        if str(f.get("name", "")).lower() == str(target_filename or "").lower():
            target = f
            break
    if not target:
        raise FileNotFoundError(
            f"Không tìm thấy '{target_filename}' trong folder {folder_id}"
        )

    _log(
        f"Tải prompt file từ Drive: folder_id={folder_id} | "
        f"filename={target_filename} | file_id={target['id']}"
    )
    req = drive_svc.files().get_media(fileId=target["id"])
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8")


def _upload_images_to_drive_folder(
    drive_svc: Any,
    parent_folder_id: str,
    folder_name: str,
    image_paths: list[Path],
    retry_attempts: int = 3,
    retry_sleep_sec: float = 1.2,
    char_image_paths: list[Path] | None = None,
) -> str:
    """
    Upload toàn bộ tệp (ảnh/zip) lên Drive vào 1 subfolder và trả link folder.

    Request gửi đi:
    1) files.create(mimeType=folder) để tạo folder.
    2) files.create(media_body=...) để upload từng tệp.
    3) permissions.create (anyone/reader) để đảm bảo link public.

    - image_paths: tệp ảnh/zip đặt trực tiếp trong folder kết quả của dòng.
    - char_image_paths: ảnh nhân vật, đặt trong folder con 'characters' RIÊNG
      (không nén) nằm cạnh file zip 150 ảnh.

    Response nhận về:
    - Link folder Drive chứa các tệp đã upload.
    """
    from googleapiclient.http import MediaFileUpload

    def _upload_one(img: Path, dest_folder_id: str) -> None:
        mime = mimetypes.guess_type(str(img))[0] or "application/octet-stream"
        media = MediaFileUpload(str(img), mimetype=mime, resumable=False)
        created = _retry_google_call(
            lambda: drive_svc.files()
            .create(
                body={"name": img.name, "parents": [dest_folder_id]},
                media_body=media,
                fields="id",
            )
            .execute(),
            attempts=retry_attempts,
            sleep_sec=retry_sleep_sec,
        )
        _log(f"Upload tệp: {img.name} -> file_id={created['id']}")
        _retry_google_call(
            lambda: drive_svc.permissions()
            .create(
                fileId=str(created["id"]),
                body={"type": "anyone", "role": "reader"},
            )
            .execute(),
            attempts=retry_attempts,
            sleep_sec=retry_sleep_sec,
        )

    folder = _retry_google_call(
        lambda: drive_svc.files()
        .create(
            body={
                "name": str(folder_name or "flow_images").strip(),
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_folder_id],
            },
            fields="id,name",
        )
        .execute(),
        attempts=retry_attempts,
        sleep_sec=retry_sleep_sec,
    )
    folder_id = str(folder["id"])
    folder_link = f"https://drive.google.com/drive/folders/{folder_id}"
    _log(
        f"Tạo folder upload Drive: name='{folder_name}' | "
        f"parent='{parent_folder_id}' | folder_id={folder_id}"
    )

    _retry_google_call(
        lambda: drive_svc.permissions()
        .create(fileId=folder_id, body={"type": "anyone", "role": "reader"})
        .execute(),
        attempts=retry_attempts,
        sleep_sec=retry_sleep_sec,
    )

    for img in image_paths:
        _upload_one(img, folder_id)

    # Ảnh nhân vật → folder con 'characters' riêng (không nén).
    char_imgs = [p for p in (char_image_paths or []) if p.exists() and p.is_file()]
    if char_imgs:
        char_folder = _retry_google_call(
            lambda: drive_svc.files()
            .create(
                body={
                    "name": CHARACTERS_SUBDIR,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [folder_id],
                },
                fields="id,name",
            )
            .execute(),
            attempts=retry_attempts,
            sleep_sec=retry_sleep_sec,
        )
        char_folder_id = str(char_folder["id"])
        _log(f"Tạo folder con '{CHARACTERS_SUBDIR}' Drive: folder_id={char_folder_id}")
        _retry_google_call(
            lambda: drive_svc.permissions()
            .create(fileId=char_folder_id, body={"type": "anyone", "role": "reader"})
            .execute(),
            attempts=retry_attempts,
            sleep_sec=retry_sleep_sec,
        )
        for img in char_imgs:
            _upload_one(img, char_folder_id)

    return folder_link


async def _generate_scene_images_with_flow_for_prompt_file(
    prompt_file: Path,
    worker_cfgs: list[WorkerConfig],
    scenario_name: str,
    scene_timeout_per_prompt_sec: int = 180,
    scenario_timeout_sec: int = 30 * 60,
    scene_min_success_images: int = 120,
    scene_retry_failed_rounds: int = 2,
) -> list[Path]:
    """
    Chạy luồng Flow scene-only cho 1 file prompt_image.txt.

    Ảnh output trả về dưới dạng list Path để upload Drive.
    """
    # Import lazy: chỉ nạp engine Flow (Playwright) khi thực sự chạy Flow.
    from models.flow_settings import FlowSettings
    from models.image_job import ImageJob
    from services.flow_scene_generate_service import generate_scene_images_from_job
    from services.flow_humanize_service import UnusualActivityDetectedError
    from services.flow_settings_service import (
        apply_flow_generation_settings_panel,
        load_flow_ui_settings,
    )
    from services.worker_pool_service import WorkerPool

    ui_cfg = load_flow_ui_settings()
    scene_mode = str(ui_cfg.get("scene_execution_mode", "serial") or "serial").strip().lower()
    max_in_flight = max(1, int(ui_cfg.get("pipeline_max_in_flight", 2) or 2))
    gap_min = float(ui_cfg.get("pipeline_send_gap_min", 1.5) or 1.5)
    gap_max = float(ui_cfg.get("pipeline_send_gap_max", 3.5) or 3.5)

    settings = FlowSettings(
        auto_apply=bool(ui_cfg.get("auto_apply", True)),
        top_mode=str(ui_cfg.get("top_mode", "image") or "image"),
        secondary_mode=str(ui_cfg.get("secondary_mode", "") or ""),
        aspect_ratio=str(ui_cfg.get("aspect_ratio", "16:9") or "16:9"),
        multiplier=str(ui_cfg.get("multiplier", "x1") or "x1"),
        model_name=str(ui_cfg.get("model_name", "Nano Banana 2") or "Nano Banana 2"),
        allow_model_alias_fallback=bool(ui_cfg.get("allow_model_alias_fallback", False)),
    )

    all_prompts = parse_image_prompts_file(str(prompt_file))
    if not all_prompts:
        raise RuntimeError(f"File prompt không có dữ liệu hợp lệ: {prompt_file}")
    if not worker_cfgs:
        raise RuntimeError("Không có worker nào để chạy failover.")

    scenario_dir = prompt_file.parent
    out_dir = scenario_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    async def _worker_func(page, current_job: ImageJob):
        # Bước apply panel settings được gọi ngay trước khi gửi prompt.
        if current_job.settings and current_job.settings.auto_apply:
            await apply_flow_generation_settings_panel(
                page=page,
                top_mode=current_job.settings.top_mode,
                secondary_mode=current_job.settings.secondary_mode,
                aspect_ratio=current_job.settings.aspect_ratio,
                multiplier=current_job.settings.multiplier,
                model_name=current_job.settings.model_name,
                allow_model_alias_fallback=current_job.settings.allow_model_alias_fallback,
            )
        return await generate_scene_images_from_job(page, current_job)

    total_prompts = len(all_prompts)
    last_error: Exception | None = None
    blocked_indices: set[int] = set()

    for worker_no, worker_cfg in enumerate(worker_cfgs, start=1):
        done_indices = _collect_completed_scene_indices(out_dir, total_prompts)
        pending_indices = [
            i for i in range(1, total_prompts + 1) if i not in done_indices and i not in blocked_indices
        ]
        if not pending_indices:
            _log(
                f"Resume: không còn prompt cần chạy trước worker {worker_cfg.worker_id} | "
                f"done={len(done_indices)}/{total_prompts} | policy_blocked={len(blocked_indices)}."
            )
            return _list_local_output_images(out_dir)

        pending_prompts = [all_prompts[i - 1] for i in pending_indices]
        _log(
            f"Failover run {worker_no}/{len(worker_cfgs)} | worker={worker_cfg.worker_id} | "
            f"pending={len(pending_prompts)}/{total_prompts} | "
            f"range={pending_indices[0]}..{pending_indices[-1]}"
        )

        job = ImageJob(
            job_id=scenario_name,
            prompts=pending_prompts,
            output_dir=str(out_dir),
            reference_images=None,
            settings=settings,
            metadata={
                "scenario_dir": str(scenario_dir),
                "scene_execution_mode": scene_mode,
                "pipeline_max_in_flight": max_in_flight,
                "pipeline_send_gap_min": gap_min,
                "pipeline_send_gap_max": gap_max,
                "pipeline_send_gap_sec": (gap_min + gap_max) / 2,
                # Truyền danh sách prompt blocked từ các lượt trước để khỏi chạy lại.
                "policy_blocked_prompt_indices": sorted(blocked_indices),
                # Cấu hình failover theo unusual:
                "pipeline_failover_on_unusual": True,
                # Mapping prompt gốc để đặt tên đúng canh_XXX khi resume.
                "scene_prompt_indices": pending_indices,
                "scene_prompt_total": total_prompts,
                # Đồng bộ timeout/target/retry với main_runner_no_reference.
                "scene_timeout_per_prompt_sec": int(scene_timeout_per_prompt_sec or 180),
                "scenario_timeout_sec": int(scenario_timeout_sec or (30 * 60)),
                "scene_min_success_images": len(pending_prompts),
                "scene_retry_failed_rounds": int(scene_retry_failed_rounds or 2),
            },
        )

        pool = WorkerPool(configs=[worker_cfg])
        try:
            await pool.start_all()
            await pool.run_jobs_parallel([job], _worker_func)
        finally:
            await pool.stop_all()

        worker_error = (job.metadata or {}).get("worker_error")
        if worker_error:
            err_type = str((worker_error or {}).get("type", "") or "")
            err_msg = str((worker_error or {}).get("message", "") or "")
            _log(
                f"Worker {worker_cfg.worker_id} báo lỗi: {err_type}: {err_msg or '(không có message)'}"
            )
            if err_type == "UnusualActivityDetectedError":
                last_error = UnusualActivityDetectedError(err_msg or "unusual activity")
                # Chuyển worker khác, giữ nguyên ảnh đã tạo.
                continue
            last_error = RuntimeError(f"{err_type}: {err_msg}".strip(": "))
            continue

        policy_blocked_after = set()
        for x in list((job.metadata or {}).get("policy_blocked_prompt_indices") or []):
            try:
                v = int(x)
            except Exception:
                continue
            if 1 <= v <= total_prompts:
                policy_blocked_after.add(v)
        if policy_blocked_after:
            new_blocked = policy_blocked_after - blocked_indices
            blocked_indices |= policy_blocked_after
            if new_blocked:
                _log(
                    f"Worker {worker_cfg.worker_id}: ghi nhận policy_blocked +{len(new_blocked)} "
                    f"(tổng={len(blocked_indices)})."
                )

        done_after = _collect_completed_scene_indices(out_dir, total_prompts)
        _log(
            f"Worker {worker_cfg.worker_id}: hoàn tất thêm "
            f"{max(0, len(done_after) - len(done_indices))} ảnh | "
            f"done={len(done_after)}/{total_prompts} | policy_blocked={len(blocked_indices)}"
        )
        if len(done_after) + len(blocked_indices) >= total_prompts:
            return _list_local_output_images(out_dir)

    final_images = _list_local_output_images(out_dir)
    if len(_collect_completed_scene_indices(out_dir, total_prompts)) + len(blocked_indices) >= total_prompts:
        return final_images

    if last_error:
        raise RuntimeError(
            f"Đã failover hết {len(worker_cfgs)} worker nhưng chưa xong đủ ảnh. "
            f"Hiện có {len(final_images)} file | policy_blocked={len(blocked_indices)} | "
            f"target={total_prompts}. "
            f"Lỗi cuối: {last_error}"
        )
    raise RuntimeError(
        f"Đã chạy hết {len(worker_cfgs)} worker nhưng chưa đủ ảnh. "
        f"Hiện có {len(final_images)} file | policy_blocked={len(blocked_indices)} | "
        f"target={total_prompts}."
    )


async def _generate_images_dispatch(
    config: "SheetFlowConfig",
    prompt_file: Path,
    worker_cfgs: list[WorkerConfig],
    scenario_name: str,
    row_index: int,
) -> list[Path]:
    """
    Chọn engine tạo ảnh theo config.generator và row_index.

    - "flow"    → luôn dùng Google Flow
    - "imagen4" → luôn dùng ChichBong Imagen4
    - "auto"    → xen kẽ (row_index lẻ=flow, chẵn=imagen4);
                  fallback sang engine kia nếu engine chính lỗi
    """
    use_flow = config.generator == "flow"
    use_imagen4 = config.generator == "imagen4"
    is_auto = config.generator == "auto"

    if is_auto:
        use_flow = (row_index % 2 == 1)
        use_imagen4 = not use_flow

    async def _run_flow() -> list[Path]:
        return await _generate_scene_images_with_flow_for_prompt_file(
            prompt_file=prompt_file,
            worker_cfgs=worker_cfgs,
            scenario_name=scenario_name,
            scene_timeout_per_prompt_sec=config.scene_timeout_per_prompt_sec,
            scenario_timeout_sec=config.scenario_timeout_sec,
            scene_min_success_images=config.scene_min_success_images,
            scene_retry_failed_rounds=config.scene_retry_failed_rounds,
        )

    async def _run_imagen4() -> list[Path]:
        from services.chichbong_imagen_service import generate_images_with_chichbong
        from services.prompt_service import parse_image_prompts_file

        license_key = _resolve_imagen4_license_key(config.imagen4_license_key)
        if not license_key:
            raise RuntimeError(
                "Không tự dò được imagen4 license key. "
                "Hãy truyền --imagen4-license-key hoặc set ENV IMAGEN4_LICENSE_KEY."
            )

        output_dir = prompt_file.parent / "output"

        prompts = [str(p).strip() for p in parse_image_prompts_file(str(prompt_file)) if str(p).strip()]
        if not prompts:
            raise RuntimeError("Không có prompt nào trong file.")

        # Resume: chỉ dùng lại ảnh cũ khi đã đủ số lượng prompt
        existing = _list_local_output_images(output_dir)
        if len(existing) >= len(prompts):
            _log(f"[imagen4] Resume: đã có đủ {len(existing)}/{len(prompts)} ảnh, dùng lại không gọi API.")
            return existing
        if existing:
            _log(f"[imagen4] Ảnh cũ chỉ có {len(existing)}/{len(prompts)} — xóa và generate lại toàn bộ.")
            _cleanup_local_output_images(output_dir)

        return await generate_images_with_chichbong(
            prompts=prompts,
            output_dir=output_dir,
            license_key=license_key,
            aspect_ratio=config.imagen4_aspect_ratio,
            max_in_flight=int(config.imagen4_max_in_flight or 6),
            use_legacy_model=bool(config.imagen4_use_legacy_model),
            seed=(int(config.imagen4_seed) if int(config.imagen4_seed or 0) > 0 else None),
            upscale_mode=str(config.imagen4_quality or "2k"),
            image_model_name=(str(config.imagen4_image_model_name or "").strip() or None),
        )

    if use_flow and not is_auto:
        return await _run_flow()
    if use_imagen4 and not is_auto:
        return await _run_imagen4()

    # auto mode: thử engine chính, fallback sang engine kia nếu lỗi
    primary_name, primary_fn = ("flow", _run_flow) if use_flow else ("imagen4", _run_imagen4)
    fallback_name, fallback_fn = ("imagen4", _run_imagen4) if use_flow else ("flow", _run_flow)

    try:
        _log(f"[auto] row={row_index} → engine={primary_name}")
        return await primary_fn()
    except Exception as primary_err:
        _log(f"[auto] row={row_index} engine={primary_name} lỗi: {primary_err} | fallback → {fallback_name}")
        return await fallback_fn()


async def _generate_character_images(
    config: "SheetFlowConfig",
    scenario_dir: Path,
    output_dir: Path,
) -> list[Path]:
    """
    Tạo ảnh nhân vật từ character_prompts.txt (nếu có) bằng ChichBong Imagen4.

    - Đọc file character_prompts.txt trong folder kịch bản (format LABEL: prompt).
    - Tạo mỗi prompt thành 1 ảnh, lưu vào output/characters/<LABEL>.jpg.
    - File không tồn tại / rỗng → trả [] (bỏ qua, không lỗi).
    - Resume: nếu đã đủ ảnh thì dùng lại, không gọi API.

    Trả về list[Path] ảnh nhân vật đã tạo.
    """
    char_file = scenario_dir / SCENARIO_CHARACTER_PROMPTS_FILE
    if not char_file.exists():
        return []

    char_map = parse_character_file(str(char_file))
    entries = [(label, prompt) for label, prompt in char_map.items() if str(prompt).strip()]
    if not entries:
        _log(f"[char] {char_file.name} không có prompt nhân vật hợp lệ — bỏ qua.")
        return []

    char_dir = output_dir / CHARACTERS_SUBDIR
    char_dir.mkdir(parents=True, exist_ok=True)

    # Resume: chỉ dùng lại khi đã đủ số ảnh nhân vật.
    existing = _list_local_output_images(char_dir)
    if len(existing) >= len(entries):
        _log(f"[char] Resume: đã đủ {len(existing)}/{len(entries)} ảnh nhân vật, không gọi API.")
        return existing
    if existing:
        _log(f"[char] Ảnh nhân vật cũ chỉ {len(existing)}/{len(entries)} — xóa và tạo lại toàn bộ.")
        _cleanup_local_output_images(char_dir)

    license_key = _resolve_imagen4_license_key(config.imagen4_license_key)
    if not license_key:
        _log("[char] Không có imagen4 license key — bỏ qua tạo ảnh nhân vật.")
        return []

    from services.chichbong_imagen_service import generate_character_images_with_chichbong

    _log(f"[char] Tạo {len(entries)} ảnh nhân vật → {char_dir}")
    saved = await generate_character_images_with_chichbong(
        entries=entries,
        output_dir=char_dir,
        license_key=license_key,
        aspect_ratio=config.imagen4_aspect_ratio,
        use_legacy_model=bool(config.imagen4_use_legacy_model),
        seed=(int(config.imagen4_seed) if int(config.imagen4_seed or 0) > 0 else None),
        upscale_mode=str(config.imagen4_quality or "2k"),
        image_model_name=(str(config.imagen4_image_model_name or "").strip() or None),
    )
    _log(f"[char] Hoàn tất: {len(saved)}/{len(entries)} ảnh nhân vật.")
    return saved


def run_sheet_drive_flow_pipeline(config: SheetFlowConfig) -> dict[str, Any]:
    """
    Hàm public duy nhất để chạy toàn bộ pipeline.

    Luồng request/response tổng quan:
    1) Request Sheets API đọc danh sách dòng cần xử lý.
       Response: nhận các dòng có link folder prompt.
    2) Request Drive API tải image_prompts.txt cho từng dòng.
       Response: prompt text theo từng cảnh.
    3) Request nội bộ Flow runner gửi prompt tạo ảnh.
       Response: ảnh local trong thư mục output.
    4) Request Drive API upload ảnh + tạo folder kết quả.
       Response: link folder ảnh Drive.
    5) Request Sheets API update cột output.
       Response: Sheet được cập nhật link theo đúng dòng.
    """
    cred_path = Path(config.credentials)
    token_path = Path(config.token_file)
    if not cred_path.exists():
        raise FileNotFoundError(f"Không tìm thấy credentials: {cred_path}")
    if not str(config.drive_output_parent_id or "").strip():
        raise ValueError("Thiếu drive_output_parent_id")
    if str(config.drive_output_parent_id).strip() in {
        "ID_FOLDER_DRIVE_THAT",
        "DRIVE_PARENT_FOLDER_ID_CAN_GHI_ANH",
    }:
        raise ValueError(
            "drive_output_parent_id đang là placeholder. Hãy thay bằng folder ID thật trên Drive."
        )

    sheets_svc, drive_svc = _build_google_services(cred_path, token_path)
    _log("Auth Google thành công (Sheets + Drive).")

    sheet_id = _extract_sheet_id(config.sheet)
    meta = _retry_google_call(
        lambda: sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute(),
        attempts=config.google_retry_attempts,
        sleep_sec=config.google_retry_sleep_sec,
    )
    tab_name = meta["sheets"][0]["properties"]["title"]
    sheet_title = meta.get("properties", {}).get("title", sheet_id)
    _log(f"Đọc Sheet: '{sheet_title}' | tab='{tab_name}' | sheet_id={sheet_id}")

    rows, hidden_links = _read_sheet_rows_with_hidden_links(
        sheets_svc=sheets_svc,
        spreadsheet_id=sheet_id,
        tab_name=tab_name,
        columns=config.range_columns,
        max_rows=config.max_rows,
        retry_attempts=config.google_retry_attempts,
        retry_sleep_sec=config.google_retry_sleep_sec,
    )
    if not rows:
        raise RuntimeError("Sheet không có dữ liệu.")
    _log(f"Tải dữ liệu sheet: rows={len(rows)} | hidden_links={len(hidden_links)}")

    header_idx = _find_header_row(rows, config.col_prompt_folder)
    header = rows[header_idx]
    col_prompt = _find_col(header, config.col_prompt_folder)
    col_title = _find_col(header, config.col_title)
    col_output = _find_col(header, config.col_output_folder)
    if col_prompt is None:
        raise RuntimeError(f"Không tìm thấy cột '{config.col_prompt_folder}'.")
    if col_output is None:
        raise RuntimeError(f"Không tìm thấy cột '{config.col_output_folder}'.")
    _log(
        f"Map cột: prompt='{config.col_prompt_folder}'(idx={col_prompt}) | "
        f"title='{config.col_title}'(idx={col_title}) | "
        f"output='{config.col_output_folder}'(idx={col_output})"
    )

    # Worker (Chrome profile) CHỈ cần cho engine Flow. Engine imagen4 không dùng.
    worker_cfgs: list[WorkerConfig] = []
    if config.generator != "imagen4":
        worker_rows = json.loads(Path(config.video_workers_config).read_text(encoding="utf-8"))
        workers = worker_rows.get("video_workers") or worker_rows.get("workers") or []
        if not workers:
            raise RuntimeError("config/video_workers.json không có worker.")
        for row in workers:
            cfg = WorkerConfig(
                worker_id=str(row.get("worker_id", "video_unknown")),
                profile_dir=str(row.get("profile_dir", "")),
                proxy=(str(row.get("proxy")) if config.use_proxy and row.get("proxy") else None),
            )
            if not cfg.profile_dir:
                continue
            worker_cfgs.append(cfg)
        if not worker_cfgs:
            raise RuntimeError("Không có worker hợp lệ (profile_dir rỗng).")
        _log(
            f"Dùng {len(worker_cfgs)} worker cho failover: "
            + ", ".join([w.worker_id for w in worker_cfgs])
        )
    else:
        _log("Engine imagen4 — không cần worker Chrome.")

    base_workspace = Path(config.workspace_dir)
    base_workspace.mkdir(parents=True, exist_ok=True)

    result_items: list[dict[str, Any]] = []
    ok = 0
    fail = 0

    stride = max(1, int(config.row_stride or 1))
    offset = max(0, int(config.row_offset or 0)) % stride
    data_row_counter = -1  # đếm riêng hàng dữ liệu (sau header) để tính stride
    assigned_ids: set[int] = set(config.row_ids) if config.row_ids else set()

    for r_idx in range(header_idx + 1, len(rows)):
        row = rows[r_idx]
        row_number = r_idx + 1
        if row_number < max(1, int(config.row_start or 1)):
            continue
        if int(config.row_end or 0) > 0 and row_number > int(config.row_end):
            break
        # Chế độ coordinator: chỉ xử lý hàng được phân công
        if assigned_ids and row_number not in assigned_ids:
            continue
        data_row_counter += 1
        if not assigned_ids and stride > 1 and (data_row_counter % stride) != offset:
            continue

        prompt_text = row[col_prompt].strip() if col_prompt < len(row) else ""
        prompt_hidden = hidden_links.get((r_idx, col_prompt), "").strip()
        prompt_folder_link = prompt_hidden or prompt_text
        existing_output = row[col_output].strip() if col_output < len(row) else ""
        title_text = row[col_title].strip() if (col_title is not None and col_title < len(row)) else ""

        if not prompt_folder_link:
            _log(f"Row {row_number}: bỏ qua vì 'Prompt tạo ảnh' trống.")
            result_items.append(
                {"row": row_number, "status": "skip", "reason": "empty_prompt_folder"}
            )
            continue
        if not _is_drive_link_or_id(prompt_folder_link):
            _log(
                f"Row {row_number}: bỏ qua vì 'Prompt tạo ảnh' không phải link/ID Drive hợp lệ: "
                f"'{prompt_folder_link[:80]}'"
            )
            result_items.append(
                {"row": row_number, "status": "skip", "reason": "invalid_prompt_folder_link"}
            )
            continue
        if existing_output:
            _log(f"Row {row_number}: bỏ qua vì đã có link output.")
            result_items.append(
                {"row": row_number, "status": "skip", "reason": "already_has_output"}
            )
            continue

        safe_title = _sanitize_name(title_text, fallback=f"row_{row_number}")
        scenario_name = f"{sheet_id}_r{row_number}_{safe_title}"
        scenario_dir = base_workspace / scenario_name
        scenario_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = scenario_dir / SCENARIO_IMAGE_FILE

        row_started_at = time.time()
        row_start_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _log(
                f"Row {row_number}: bắt đầu xử lý | title='{title_text[:80]}' | "
                f"prompt_folder='{prompt_folder_link[:120]}'"
            )
            prompt_content = _download_text_file_from_drive_folder(
                drive_svc=drive_svc,
                folder_link_or_id=prompt_folder_link,
                target_filename=config.drive_prompt_filename,
                retry_attempts=config.google_retry_attempts,
                retry_sleep_sec=config.google_retry_sleep_sec,
            )
            prompt_file.write_text(prompt_content, encoding="utf-8")
            _log(f"Row {row_number}: đã lưu prompt local -> {prompt_file}")

            # Tải thêm file prompt nhân vật (tùy chọn). Folder không có thì bỏ qua.
            char_prompt_file = scenario_dir / SCENARIO_CHARACTER_PROMPTS_FILE
            char_filename = str(config.drive_character_prompt_filename or "").strip()
            if char_filename:
                try:
                    char_content = _download_text_file_from_drive_folder(
                        drive_svc=drive_svc,
                        folder_link_or_id=prompt_folder_link,
                        target_filename=char_filename,
                        retry_attempts=config.google_retry_attempts,
                        retry_sleep_sec=config.google_retry_sleep_sec,
                    )
                    char_prompt_file.write_text(char_content, encoding="utf-8")
                    _log(f"Row {row_number}: đã lưu prompt nhân vật local -> {char_prompt_file}")
                except FileNotFoundError:
                    _log(f"Row {row_number}: không có '{char_filename}' trong folder — bỏ qua ảnh nhân vật.")

            prompt_scene_total = len(parse_image_prompts_file(str(prompt_file)))
            if prompt_scene_total <= 0:
                raise RuntimeError(f"Row {row_number}: prompt không có cảnh hợp lệ.")
            _log(f"Row {row_number}: tổng số cảnh trong prompt = {prompt_scene_total}")

            output_dir = scenario_dir / "output"
            # fresh_run: mỗi lần chạy là chạy MỚI → dọn sạch ảnh cũ (cả cảnh lẫn
            # nhân vật) cho mọi engine trước khi generate, không tái dùng/resume.
            # Khi tắt fresh_run: chỉ flow dọn ảnh cũ (imagen4 giữ lại để tái dùng).
            if config.fresh_run:
                deleted_old = _cleanup_local_output_images(output_dir)
                deleted_old += _cleanup_local_output_images(output_dir / CHARACTERS_SUBDIR)
                if deleted_old > 0:
                    _log(f"Row {row_number}: fresh_run — đã dọn {deleted_old} ảnh cũ trước khi generate.")
            elif config.generator != "imagen4":
                deleted_old = _cleanup_local_output_images(output_dir)
                if deleted_old > 0:
                    _log(f"Row {row_number}: đã dọn {deleted_old} ảnh cũ trong output trước khi generate.")

            local_images = asyncio.run(
                _generate_images_dispatch(
                    config=config,
                    prompt_file=prompt_file,
                    worker_cfgs=worker_cfgs,
                    scenario_name=scenario_name,
                    row_index=row_number,
                )
            )
            if not local_images:
                raise RuntimeError("Không tạo được ảnh nào (flow + imagen4 đều thất bại).")
            success_images = len(local_images)
            completed_scenes = len(_collect_completed_scene_indices(output_dir, prompt_scene_total))
            missing_scenes = max(0, prompt_scene_total - completed_scenes)
            _log(
                f"Row {row_number}: số ảnh local sẵn sàng upload = {success_images} | "
                f"cảnh hoàn tất={completed_scenes}/{prompt_scene_total} | "
                f"cảnh còn sót={missing_scenes}"
            )

            # Tạo ảnh nhân vật (nếu có character_prompts.txt) → output/characters/.
            try:
                character_images = asyncio.run(
                    _generate_character_images(
                        config=config,
                        scenario_dir=scenario_dir,
                        output_dir=output_dir,
                    )
                )
            except Exception as char_err:
                _log(f"Row {row_number}: lỗi tạo ảnh nhân vật (bỏ qua): {char_err}")
                character_images = []
            if character_images:
                _log(f"Row {row_number}: có {len(character_images)} ảnh nhân vật.")

            # Tên rút gọn tiêu đề kịch bản (~40 ký tự) dùng cho cả tên zip và folder Drive.
            short_title = _sanitize_name(title_text, fallback="")[:40].strip("_")

            # Nén ảnh cảnh thành 1 file zip để giảm số request upload và tăng tốc.
            # Ảnh nhân vật KHÔNG nén — upload thành folder characters/ riêng trên Drive.
            zip_name = short_title or f"row_{row_number}"
            zip_path = _build_zip_from_images(
                image_paths=local_images,
                zip_path=scenario_dir / f"{zip_name}.zip",
            )
            _log(f"Row {row_number}: đã nén ảnh -> {zip_path.name}")

            # Tên folder Drive: rút gọn tiêu đề kịch bản (~40 ký tự) + số dòng.
            drive_folder_name = f"{short_title}_r{row_number}" if short_title else f"row_{row_number}"
            drive_folder_link = _upload_images_to_drive_folder(
                drive_svc=drive_svc,
                parent_folder_id=str(config.drive_output_parent_id).strip(),
                folder_name=drive_folder_name,
                image_paths=[zip_path],
                retry_attempts=config.google_retry_attempts,
                retry_sleep_sec=config.google_retry_sleep_sec,
                char_image_paths=character_images,
            )

            cell = f"'{tab_name}'!{_col_index_to_a1(col_output)}{row_number}"
            _retry_google_call(
                lambda: sheets_svc.spreadsheets()
                .values()
                .update(
                    spreadsheetId=sheet_id,
                    range=cell,
                    valueInputOption="RAW",
                    body={"values": [[drive_folder_link]]},
                )
                .execute(),
                attempts=config.google_retry_attempts,
                sleep_sec=config.google_retry_sleep_sec,
            )
            _log(f"Row {row_number}: ghi link về sheet thành công -> {cell}")
            _log(
                "Thông báo: "
                f"ID sheet đã điền: {sheet_id} | "
                f"Linkk Drive đã điền: {drive_folder_link} | "
                f"điền ở dòng thứ mấy: {row_number} | "
                f"thành công bao nhiêu ảnh: {success_images} | "
                f"còn sót bao nhiêu cảnh chưa có: {missing_scenes}"
            )

            elapsed = time.time() - row_started_at
            _log(
                f"Row {row_number}: hoàn tất kịch bản trong {_format_duration(elapsed)} "
                f"({elapsed:.1f}s)"
            )
            _append_scenario_timing(
                config.workspace_dir,
                f"[{row_start_str}] sheet={sheet_id} | row={row_number} | "
                f"title='{title_text[:60]}' | status=ok | "
                f"images={success_images} | scenes={completed_scenes}/{prompt_scene_total} | "
                f"thoi_gian={_format_duration(elapsed)} ({elapsed:.1f}s)",
            )

            # Đã upload Drive + ghi sheet xong → xóa toàn bộ thư mục kịch bản local
            # để giải phóng ổ đĩa và đảm bảo lần chạy sau là chạy mới.
            if config.delete_local_after_done:
                try:
                    shutil.rmtree(scenario_dir, ignore_errors=True)
                    _log(f"Row {row_number}: đã xóa local sau khi hoàn tất -> {scenario_dir}")
                except Exception as cleanup_err:
                    _log(f"Row {row_number}: không xóa được local (bỏ qua): {cleanup_err}")

            ok += 1
            result_items.append(
                {
                    "row": row_number,
                    "status": "ok",
                    "images": success_images,
                    "target_scenes": prompt_scene_total,
                    "completed_scenes": completed_scenes,
                    "missing_scenes": missing_scenes,
                    "sheet_cell": cell,
                    "zip_file": str(zip_path),
                    "drive_folder": drive_folder_link,
                    "scenario_dir": str(scenario_dir),
                    "elapsed_sec": round(elapsed, 1),
                    "elapsed_human": _format_duration(elapsed),
                }
            )
        except Exception as exc:
            elapsed = time.time() - row_started_at
            fail += 1
            _log(
                f"Row {row_number}: lỗi sau {_format_duration(elapsed)} "
                f"({elapsed:.1f}s) -> {exc}"
            )
            _append_scenario_timing(
                config.workspace_dir,
                f"[{row_start_str}] sheet={sheet_id} | row={row_number} | "
                f"title='{title_text[:60]}' | status=error | "
                f"thoi_gian={_format_duration(elapsed)} ({elapsed:.1f}s) | loi={exc}",
            )
            result_items.append(
                {"row": row_number, "status": "error", "error": str(exc),
                 "elapsed_sec": round(elapsed, 1), "elapsed_human": _format_duration(elapsed)}
            )

    return {
        "sheet_id": sheet_id,
        "sheet_title": sheet_title,
        "ok": ok,
        "fail": fail,
        "items": result_items,
    }
