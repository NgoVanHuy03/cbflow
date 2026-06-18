from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import parse_qs, urlparse


def _safe_prefix_for_glob(text: str, max_len: int = 50) -> str:
    """
    Chuẩn hóa prefix theo đúng quy tắc đặt tên của flow_image_service.download_flow_images.

    Lý do:
    - download_flow_images luôn "sanitize + hash" prefix trước khi ghi file.
    - Nếu map service glob bằng prefix thô thì sẽ không tìm thấy file đã tải.
    - Kết quả là log có thể hiện "saved>0" nhưng "generated_files=0".
    """
    raw_text = str(text or "")
    name = re.sub(r"[^\w\sàáảãạăắặẳẵầấẩẫậêếệểễôốộổỗơớợởỡùúủũụưứựửữđ]", "", raw_text, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name.strip())
    import hashlib
    digest = hashlib.sha1(raw_text.encode("utf-8")).hexdigest()[:10]
    if not name:
        return f"prompt_{digest}"
    keep = max(1, int(max_len) - (len(digest) + 1))
    return f"{name[:keep]}_{digest}"


@dataclass
class PromptMediaBatch:
    """
    Kết quả map media của 1 lần gửi prompt.

    Ý nghĩa:
    - 1 prompt có thể tạo ra N ảnh.
    - Service này gom toàn bộ ảnh của prompt đó thành 1 "batch" duy nhất.
    - Các stage (reference/scene) chỉ cần dùng batch này thay vì tự map riêng.
    """
    mode: str
    prompt_index: int
    prompt_total: int
    prompt_text_preview: str
    prefix: str
    output_dir: str
    expected_count: int
    saved_count: int
    new_srcs: list[str]
    normalized_new_srcs: list[str]
    media_ids: list[str]
    generated_files: list[str]
    ok: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Đổi sang dict để dễ log/report."""
        return asdict(self)


def _glob_generated_files(output_dir: str, prefix: str) -> list[str]:
    """
    Quét các file ảnh được lưu theo quy ước của run_flow_image_capture:
    - <prefix>_img1.png
    - <prefix>_img2.png
    - ...
    """
    # Dùng cùng rule "safe prefix" như lúc ghi file để không bị lệch tên.
    safe_prefix = _safe_prefix_for_glob(prefix)
    pattern = os.path.join(output_dir, f"{safe_prefix}_img*.png")
    return glob.glob(pattern)


def _sort_files_for_pick(files: list[str]) -> list[str]:
    """
    Sắp xếp file theo ưu tiên chọn ảnh chính.

    Rule:
    1) File lớn hơn ưu tiên trước (thường chất lượng tốt hơn placeholder nhỏ).
    2) Nếu cùng size, file mới hơn ưu tiên trước.
    """
    def _key(path: str):
        try:
            st = os.stat(path)
            return (int(st.st_size), float(st.st_mtime))
        except Exception:
            return (0, 0.0)

    # reverse=True: size lớn + mtime mới sẽ đứng đầu.
    return sorted(files, key=_key, reverse=True)


def normalize_media_url(url: str) -> str:
    """
    Chuẩn hóa URL để so khớp ổn định.

    Quy tắc quan trọng:
    - Nếu là redirect URL `media.getMediaUrlRedirect?name=...` thì GIỮ query `name`.
    - Các URL khác: bỏ query string để giảm nhiễu token/timestamp.
    """
    s = str(url or "").strip()
    if not s:
        return ""

    low = s.lower()
    if "media.getmediaurlredirect" in low and "name=" in low:
        try:
            u = urlparse(s)
            q = parse_qs(u.query or "")
            name = (q.get("name", [""])[0] or "").strip()
            if name:
                return f"{u.scheme}://{u.netloc}{u.path}?name={name}"
        except Exception:
            return s
    return s.split("?", 1)[0]


def extract_media_id_from_redirect_url(url: str) -> str:
    """
    Tách media_id từ URL redirect:
    `.../media.getMediaUrlRedirect?name=<media_id>`
    """
    s = str(url or "").strip()
    if not s:
        return ""
    if "media.getMediaUrlRedirect" not in s:
        return ""
    try:
        u = urlparse(s)
        q = parse_qs(u.query or "")
        return str((q.get("name", [""])[0] or "")).strip()
    except Exception:
        return ""


def collect_media_ids_from_obj(obj, out_media_ids: set[str]) -> None:
    """
    Duyệt JSON để gom media_id/mediaId/name có dạng UUID-ish.
    """
    uuid_like = re.compile(r"^[0-9a-fA-F-]{16,}$")
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in {"media_id", "mediaid", "primarymediaid", "name", "id"} and isinstance(v, (str, int)):
                sv = str(v).strip()
                if uuid_like.match(sv):
                    out_media_ids.add(sv)
            collect_media_ids_from_obj(v, out_media_ids)
    elif isinstance(obj, list):
        for x in obj:
            collect_media_ids_from_obj(x, out_media_ids)


def build_prompt_media_batch(
    *,
    mode: str,
    prompt_index: int,
    prompt_total: int,
    prompt_text: str,
    output_dir: str,
    prefix: str,
    expected_count: int,
    capture_result: dict | None,
) -> PromptMediaBatch:
    """
    Dựng batch map media cho 1 prompt.

    Request vào:
    - Kết quả capture hiện tại (`capture_result`) + metadata của prompt.

    Response ra:
    - PromptMediaBatch chứa đầy đủ:
      + URL mới (`new_srcs`)
      + file đã lưu trên disk (`generated_files`)
      + số lượng ảnh thực tế của prompt này
    """
    cap = capture_result or {}
    new_srcs = list(cap.get("new_srcs", []) or [])
    normalized_new_srcs = []
    media_ids = []
    seen_norm = set()
    seen_mid = set()
    for src in new_srcs:
        nu = normalize_media_url(src)
        if nu and nu not in seen_norm:
            seen_norm.add(nu)
            normalized_new_srcs.append(nu)
        mid = extract_media_id_from_redirect_url(src)
        if mid and mid not in seen_mid:
            seen_mid.add(mid)
            media_ids.append(mid)
    saved_count = int(cap.get("saved", 0) or 0)
    generated_files = _sort_files_for_pick(_glob_generated_files(output_dir, prefix))
    ok = bool(cap.get("ok")) and (saved_count > 0 or len(new_srcs) > 0 or len(generated_files) > 0)

    return PromptMediaBatch(
        mode=str(mode or "").strip() or "unknown",
        prompt_index=int(prompt_index),
        prompt_total=int(prompt_total),
        prompt_text_preview=(str(prompt_text or "").strip()[:180]),
        prefix=prefix,
        output_dir=output_dir,
        expected_count=max(1, int(expected_count or 1)),
        saved_count=saved_count,
        new_srcs=new_srcs,
        normalized_new_srcs=normalized_new_srcs,
        media_ids=media_ids,
        generated_files=generated_files,
        ok=ok,
        note=(
            f"expected={max(1, int(expected_count or 1))}, "
            f"srcs={len(new_srcs)}, norm_srcs={len(normalized_new_srcs)}, "
            f"media_ids={len(media_ids)}, files={len(generated_files)}"
        ),
    )


def pick_primary_generated_file(batch: PromptMediaBatch) -> str:
    """
    Chọn 1 ảnh "primary" từ batch.

    Dùng cho luồng reference:
    - 1 prompt nhân vật có thể ra nhiều ảnh.
    - Cần chọn 1 ảnh đại diện để ghi thành CHARxx.png.

    Rule hiện tại: chọn file đứng đầu theo sort ưu tiên size lớn + file mới.
    """
    if not batch.generated_files:
        return ""
    return batch.generated_files[0]


def map_prompt_to_srcs_by_media_id(
    prompt_to_media_ids: dict[int, set[str]] | dict[str, set[str]],
    new_srcs: list[str],
) -> dict:
    """
    Map prompt -> danh sách src bằng khóa trung gian media_id.

    Request:
    - `prompt_to_media_ids`: map prompt_index -> {media_id,...} từ API.
    - `new_srcs`: danh sách URL ảnh đã bắt/tải ở phiên hiện tại.

    Response:
    - `by_prompt`: prompt_index -> [src...]
    - `unknown_srcs`: src chưa map được vào prompt nào.
    - `unknown_media_ids`: media_id có trong src nhưng không thấy trong prompt map.
    """
    media_id_to_src: dict[str, str] = {}
    for src in (new_srcs or []):
        mid = extract_media_id_from_redirect_url(src)
        if mid:
            media_id_to_src[mid] = src

    by_prompt: dict[int, list[str]] = {}
    assigned_media_ids: set[str] = set()
    for k, mids in (prompt_to_media_ids or {}).items():
        try:
            pidx = int(k)
        except Exception:
            continue
        rows = []
        for mid in sorted(mids or []):
            if mid in media_id_to_src:
                rows.append(media_id_to_src[mid])
                assigned_media_ids.add(mid)
        by_prompt[pidx] = rows

    unknown_srcs = []
    unknown_media_ids = []
    for mid, src in media_id_to_src.items():
        if mid not in assigned_media_ids:
            unknown_media_ids.append(mid)
            unknown_srcs.append(src)

    return {
        "by_prompt": by_prompt,
        "unknown_srcs": unknown_srcs,
        "unknown_media_ids": unknown_media_ids,
        "note": "media_id-first mapping",
    }
