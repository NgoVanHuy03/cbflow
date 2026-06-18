#!/usr/bin/env python3
"""
Live integration test cho phần tải ảnh + kiểm tra quy tắc đặt tên file.

Mục tiêu của script:
- Chạy thật với Chrome profile thật (không mock).
- Gửi prompt thật lên Google Flow.
- Tải ảnh thật bằng service hiện có.
- Kiểm tra chi tiết file đã tải có đúng chuẩn tên hay không.

Request script gửi đi (dễ hiểu):
1) Mở Chrome bằng profile của worker từ config/video_workers.json.
2) Mở tab Flow project mới.
3) Với từng prompt: gửi prompt -> chờ ảnh render -> tải ảnh về thư mục output.

Response script nhận về (dễ hiểu):
- URL ảnh mới từ Flow (new_srcs).
- Số ảnh service báo đã lưu (saved_count).
- Danh sách file thực tế trong ổ đĩa.
- Kết quả kiểm tra chuẩn tên file + kích thước + định dạng ảnh hợp lệ.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

from models.worker_config import WorkerConfig
from services.flow_image_service import (
    _safe_filename,
    capture_flow_baseline_srcs,
    download_flow_images,
    wait_for_flow_images,
)
from services.flow_prompt_service import (
    click_flow_generate_button,
    find_and_focus_flow_prompt,
    send_flow_prompt,
    type_flow_prompt,
)
from services.worker_pool_service import WorkerPool


def _normalize_scenario_name(raw: str | None) -> str | None:
    """Chuẩn hóa scenario kiểu A -> kich_ban_A để log dễ đọc."""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith("kich_ban_"):
        return text
    return f"kich_ban_{text}"


def _load_worker_profile_from_config(worker_id: str) -> tuple[str, str | None]:
    """
    Đọc profile/proxy của worker từ config/video_workers.json.

    Trả về:
    - profile_dir: đường dẫn profile Chrome.
    - proxy: proxy string (nếu có).
    """
    cfg_path = Path("config/video_workers.json")
    if not cfg_path.exists():
        raise FileNotFoundError(f"Không tìm thấy {cfg_path}")

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    workers = data.get("video_workers", []) or data.get("workers", []) or []

    for row in workers:
        if str(row.get("worker_id", "")) != str(worker_id):
            continue
        profile_dir = str(row.get("profile_dir", "")).strip()
        if not profile_dir:
            raise ValueError(f"worker '{worker_id}' thiếu profile_dir")
        return profile_dir, row.get("proxy")

    raise ValueError(f"Không tìm thấy worker '{worker_id}' trong config/video_workers.json")


def _parse_prompt_list(raw_prompt: str, raw_prompts: list[str] | None) -> list[str]:
    """Ưu tiên --prompts, sau đó --prompt, cuối cùng dùng 3 prompt mặc định."""
    if raw_prompts:
        rows = [str(p).strip() for p in raw_prompts if str(p).strip()]
        if rows:
            return rows
    if str(raw_prompt or "").strip():
        return [str(raw_prompt).strip()]
    return [
        "CẢNH 001: chân dung studio, ánh sáng mềm, nền tối giản, photorealistic",
        "CẢNH 002: người đàn ông mặc vest đứng giữa đường phố ban đêm, cinematic",
        "CẢNH 003: cô gái ngồi gần cửa sổ, nắng chiều, ảnh tông ấm, chi tiết cao",
    ]


def _tail_text(value: str, tail_len: int = 140) -> str:
    """Rút ngắn chuỗi dài để JSON debug panel dễ đọc."""
    text = str(value or "")
    if len(text) <= max(1, int(tail_len)):
        return text
    return "..." + text[-max(1, int(tail_len)):]


def _preview_list(values: list, max_items: int = 12) -> list:
    """Lấy preview danh sách để tránh log quá dài."""
    rows = list(values or [])
    if len(rows) <= max(1, int(max_items)):
        return rows
    keep = max(1, int(max_items))
    return rows[:keep] + [f"...(+{len(rows) - keep} items)"]


def _detect_image_format(path: Path) -> str:
    """
    Nhận diện nhanh định dạng ảnh theo magic bytes.

    Trả về một trong các giá trị:
    - png / jpeg / webp / gif / bmp
    - unknown (nếu không nhận diện được)
    """
    try:
        with path.open("rb") as f:
            sig = f.read(16)
        if sig.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if sig.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        if sig.startswith(b"RIFF") and sig[8:12] == b"WEBP":
            return "webp"
        if sig.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if sig.startswith(b"BM"):
            return "bmp"
        return "unknown"
    except Exception:
        return "unknown"


def _validate_downloaded_file_names(
    *,
    output_dir: Path,
    prefix_raw: str,
    min_file_size: int,
) -> dict:
    """
    Kiểm tra chuẩn tên file do download_flow_images sinh ra.

    Chuẩn kỳ vọng:
    - Tên file có dạng: <safe_prefix>_img1.png, <safe_prefix>_img2.png, ...
    - Chỉ số img phải liên tục từ 1..N (không đứt đoạn).
    - File tồn tại, đủ kích thước tối thiểu, và là ảnh hợp lệ (png/jpeg/webp/gif/bmp).
    """
    safe_prefix = _safe_filename(prefix_raw)
    pattern = re.compile(rf"^{re.escape(safe_prefix)}_img(\d+)\.png$", flags=re.IGNORECASE)

    matched_files = sorted(output_dir.glob(f"{safe_prefix}_img*.png"))
    invalid_name_files: list[str] = []
    index_values: list[int] = []
    too_small_files: list[str] = []
    unknown_image_files: list[str] = []
    extension_mismatch_files: list[dict] = []
    detected_formats: dict[str, str] = {}

    # Duyệt từng file đã match theo wildcard để xác thực kỹ bằng regex chuẩn.
    for file_path in matched_files:
        match = pattern.match(file_path.name)
        if not match:
            invalid_name_files.append(file_path.name)
            continue

        idx = int(match.group(1) or 0)
        index_values.append(idx)

        # Kiểm tra kích thước file để bắt trường hợp tải lỗi (file quá nhỏ).
        try:
            if file_path.stat().st_size < int(min_file_size):
                too_small_files.append(file_path.name)
        except Exception:
            too_small_files.append(file_path.name)

        # Kiểm tra signature để chắc chắn file là ảnh hợp lệ.
        detected = _detect_image_format(file_path)
        detected_formats[file_path.name] = detected
        if detected == "unknown":
            unknown_image_files.append(file_path.name)
        # Ghi chú mismatch: tên .png nhưng bytes không phải png.
        if file_path.suffix.lower() == ".png" and detected not in {"png", "unknown"}:
            extension_mismatch_files.append(
                {"file": file_path.name, "declared_ext": "png", "detected_format": detected}
            )

    sorted_indexes = sorted(index_values)
    expected_indexes = list(range(1, len(sorted_indexes) + 1))
    contiguous_indexing = bool(sorted_indexes and sorted_indexes == expected_indexes)

    return {
        "safe_prefix": safe_prefix,
        "matched_files": [p.name for p in matched_files],
        "matched_count": len(matched_files),
        "index_values": sorted_indexes,
        "expected_indexes": expected_indexes,
        "invalid_name_files": invalid_name_files,
        "too_small_files": too_small_files,
        "unknown_image_files": unknown_image_files,
        "extension_mismatch_files": extension_mismatch_files,
        "detected_formats": detected_formats,
        "contiguous_indexing": contiguous_indexing,
        "name_rule_pass": (len(invalid_name_files) == 0 and contiguous_indexing),
        "file_quality_pass": (len(too_small_files) == 0 and len(unknown_image_files) == 0),
    }


def _print_debug_panel(title: str, payload: dict, enabled: bool = True) -> None:
    """In panel DEBUG dạng JSON dễ đọc."""
    if not enabled:
        return
    print("\n" + "=" * 72)
    print(f"DEBUG PANEL | {title}")
    print("=" * 72)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("=" * 72)


def _build_prompt_debug_panel(
    *,
    prompt_index: int,
    prompt_total: int,
    prompt_text: str,
    expected_count: int,
    output_dir: Path,
    prefix_raw: str,
    new_srcs: list[str],
    saved_count: int,
    naming_report: dict,
) -> dict:
    """Dựng panel debug cho từng prompt."""
    request_block = {
        "mode": "single_prompt_image_download",
        "prompt_index": int(prompt_index),
        "prompt_total": int(prompt_total),
        "prompt_text_preview": str(prompt_text or "")[:180],
        "expected_count": max(1, int(expected_count or 1)),
        "output_dir": str(output_dir),
        "prefix_raw": str(prefix_raw),
        "safe_prefix": str(naming_report.get("safe_prefix", "")),
    }

    response_block = {
        "new_srcs_count": len(new_srcs),
        "saved_count": int(saved_count),
        "new_srcs_preview": [_tail_text(x) for x in _preview_list(new_srcs)],
        "matched_file_count": int(naming_report.get("matched_count", 0) or 0),
        "matched_files_preview": _preview_list(naming_report.get("matched_files", []), max_items=20),
    }

    checks_block = {
        "has_new_srcs": len(new_srcs) > 0,
        "saved_count_positive": int(saved_count) > 0,
        "saved_match_files": int(saved_count) == int(naming_report.get("matched_count", 0) or 0),
        "name_rule_pass": bool(naming_report.get("name_rule_pass")),
        "file_quality_pass": bool(naming_report.get("file_quality_pass")),
        "invalid_name_files": naming_report.get("invalid_name_files", []),
        "missing_or_non_contiguous_indexes": {
            "actual": naming_report.get("index_values", []),
            "expected": naming_report.get("expected_indexes", []),
        },
        "too_small_files": naming_report.get("too_small_files", []),
        "unknown_image_files": naming_report.get("unknown_image_files", []),
        "extension_mismatch_files": naming_report.get("extension_mismatch_files", []),
        "detected_formats": naming_report.get("detected_formats", {}),
    }

    all_ok = (
        checks_block["has_new_srcs"]
        and checks_block["saved_count_positive"]
        and checks_block["saved_match_files"]
        and checks_block["name_rule_pass"]
        and checks_block["file_quality_pass"]
    )
    checks_block["status"] = "PASS" if all_ok else "WARN"

    return {
        "request": request_block,
        "response": response_block,
        "checks": checks_block,
    }


async def run_live_test(args: argparse.Namespace) -> int:
    """Luồng test live chính."""
    scenario_name = _normalize_scenario_name(args.scenario)
    profile_dir, proxy_from_config = _load_worker_profile_from_config(args.worker)
    prompts = _parse_prompt_list(args.prompt, args.prompts)

    # Tạo output folder riêng cho lần test để không đè dữ liệu cũ.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir).resolve() if args.output_dir else (Path("debug_sessions") / f"live_image_download_naming_{ts}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Cho phép tắt proxy khi cần debug local.
    proxy = None if args.no_proxy else proxy_from_config

    worker_cfg = WorkerConfig(worker_id=args.worker, profile_dir=profile_dir, proxy=proxy)
    pool = WorkerPool(configs=[worker_cfg])

    all_panels: list[dict] = []
    pass_count = 0

    try:
        await pool.start_all()
        context = pool.contexts[args.worker]

        # Mở tab sạch và tạo project mới để test ổn định hơn.
        await pool._close_all_existing_tabs(context)
        page = await pool._open_flow_new_project_tab(context, args.worker)

        print("\n" + "=" * 72)
        print("LIVE TEST: image download naming (worker thật)")
        print("=" * 72)
        print(f"Worker           : {args.worker}")
        print(f"Profile          : {profile_dir}")
        print(f"Proxy dùng       : {proxy or 'KHÔNG'}")
        print(f"Scenario         : {scenario_name or 'không chỉ định'}")
        print(f"Output dir       : {out_dir}")
        print(f"Số prompt test   : {len(prompts)}")
        for idx, text in enumerate(prompts, start=1):
            short = text[:120] + ("..." if len(text) > 120 else "")
            print(f"Prompt {idx:02d}       : {short}")
        print("=" * 72 + "\n")

        # Chạy từng prompt để phân tách rõ file tải theo từng lần generate.
        for idx, prompt_text in enumerate(prompts, start=1):
            print(f"\n--- PROMPT {idx}/{len(prompts)} ---")

            # Prefix raw cố tình có khoảng trắng/ký tự đặc biệt để test luôn bước sanitize.
            prefix_raw = f"live image {args.worker} {ts} prompt#{idx}"

            # Baseline trước khi gửi prompt để xác định chính xác ảnh mới.
            before_srcs = await capture_flow_baseline_srcs(page)

            # Gửi prompt thật vào ô nhập.
            await find_and_focus_flow_prompt(page)
            await type_flow_prompt(page, prompt_text)
            sent_ok = await send_flow_prompt(page)
            if not sent_ok:
                # Fallback click thủ công nút generate nếu Enter không gửi được.
                await click_flow_generate_button(page)

            # Chờ ảnh mới render dựa trên baseline vừa chụp.
            new_srcs = await wait_for_flow_images(
                page=page,
                before_srcs=before_srcs,
                expected=max(1, int(args.expected)),
                timeout=max(30, int(args.timeout)),
            )

            # Tải ảnh thật về disk bằng service hiện có.
            saved_count = await download_flow_images(
                page=page,
                new_srcs=new_srcs,
                output_dir=str(out_dir),
                prefix=prefix_raw,
                max_download=(None if int(args.max_download) <= 0 else int(args.max_download)),
            )

            # Kiểm tra quy tắc đặt tên + chất lượng file đã tải.
            naming_report = _validate_downloaded_file_names(
                output_dir=out_dir,
                prefix_raw=prefix_raw,
                min_file_size=int(args.min_file_size),
            )

            panel = _build_prompt_debug_panel(
                prompt_index=idx,
                prompt_total=len(prompts),
                prompt_text=prompt_text,
                expected_count=int(args.expected),
                output_dir=out_dir,
                prefix_raw=prefix_raw,
                new_srcs=list(new_srcs or []),
                saved_count=int(saved_count),
                naming_report=naming_report,
            )
            all_panels.append(panel)
            _print_debug_panel(f"prompt_{idx:02d}", panel, enabled=(not args.no_debug_panel))

            if panel.get("checks", {}).get("status") == "PASS":
                pass_count += 1

        # Tổng kết toàn bộ test case đã chạy.
        overall = {
            "request": {
                "worker": args.worker,
                "scenario": scenario_name,
                "prompt_count": len(prompts),
                "expected_per_prompt": max(1, int(args.expected)),
                "min_file_size": int(args.min_file_size),
                "output_dir": str(out_dir),
            },
            "response": {
                "pass_count": pass_count,
                "fail_or_warn_count": max(0, len(all_panels) - pass_count),
                "panels_status": [p.get("checks", {}).get("status", "WARN") for p in all_panels],
            },
            "checks": {
                "all_prompts_pass": pass_count == len(all_panels) and len(all_panels) > 0,
                "status": "PASS" if (pass_count == len(all_panels) and len(all_panels) > 0) else "WARN",
            },
        }
        _print_debug_panel("overall", overall, enabled=True)

        # Trả exit code rõ nghĩa để CI/script khác có thể bắt nhanh.
        return 0 if overall["checks"]["status"] == "PASS" else 2

    finally:
        await pool.stop_all()


def parse_args() -> argparse.Namespace:
    """CLI options để chạy test linh hoạt."""
    parser = argparse.ArgumentParser(
        description="Live test chi tiết tải ảnh + kiểm tra naming file ảnh"
    )
    parser.add_argument("--worker", default="video_1", help="worker_id trong config/video_workers.json")
    parser.add_argument("--scenario", default="", help="Chỉ dùng để ghi log (A/B/C...)")
    parser.add_argument("--prompt", default="", help="1 prompt duy nhất")
    parser.add_argument("--prompts", nargs="*", help="Danh sách nhiều prompt")
    parser.add_argument("--expected", type=int, default=1, help="Số ảnh kỳ vọng tối thiểu mỗi prompt")
    parser.add_argument("--timeout", type=int, default=180, help="Timeout chờ render mỗi prompt (giây)")
    parser.add_argument("--max-download", type=int, default=0, help="Giới hạn số ảnh tải mỗi prompt (<=0 = tải toàn bộ)")
    parser.add_argument("--min-file-size", type=int, default=6 * 1024, help="Ngưỡng dung lượng file tối thiểu (bytes)")
    parser.add_argument("--output-dir", default="", help="Thư mục output custom")
    parser.add_argument("--no-proxy", action="store_true", help="Không dùng proxy từ config")
    parser.add_argument("--no-debug-panel", action="store_true", help="Tắt panel debug từng prompt")
    return parser.parse_args()


def main() -> int:
    """Entry point."""
    args = parse_args()
    return asyncio.run(run_live_test(args))


if __name__ == "__main__":
    raise SystemExit(main())
