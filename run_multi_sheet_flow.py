#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_multi_sheet_flow.py

Chạy nhiều Google Sheet theo file nguồn (giống style black-auto-python).

Định dạng file sources:
  - Mỗi dòng 1 sheet URL/ID
  - Hoặc: <sheet_url_or_id>|<project_mode>
  - Dòng bắt đầu bằng # sẽ bị bỏ qua

Ví dụ:
  https://docs.google.com/spreadsheets/d/abc/edit#gid=0|black-auto
  1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890|hoa
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.sheet_drive_flow_service import SheetFlowConfig, run_sheet_drive_flow_pipeline, _extract_sheet_id


def _clean_sheet_url(sheet_url: str) -> str:
    sheet_id = _extract_sheet_id(sheet_url)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def parse_args() -> argparse.Namespace:
    """
    Parse tham số CLI.

    Mặc định tự lấy:
    - sources=sheet_sources.txt
    - credentials=credentials.json
    - token-file=token.json

    Tuỳ chọn:
    - drive-parent-hoa: folder Drive cha cho mode hoa (nếu có dòng |hoa)
    """
    p = argparse.ArgumentParser(description="Chạy nhiều sheet -> Flow -> Drive -> Sheet")
    p.add_argument("--sources", default="sheet_sources.txt", help="File nguồn kiểu sheet_url|project_mode (mặc định: sheet_sources.txt)")
    p.add_argument("--credentials", default="credentials.json", help="Credentials OAuth/Service Account (mặc định: credentials.json)")
    p.add_argument("--token-file", default="token.json", help="Token OAuth file (mặc định: token.json)")
    p.add_argument(
        "--drive-parent-black-auto",
        default="root",
        help="Drive folder ID cho mode black-auto. Mặc định: root (My Drive gốc).",
    )
    p.add_argument(
        "--drive-parent-hoa",
        default="root",
        help="Drive folder ID cho mode hoa. Mặc định: root (My Drive gốc).",
    )
    p.add_argument("--workspace-dir", default="scenarios/sheets_pipeline", help="Thư mục local lưu scenario tạm")
    p.add_argument("--video-workers-config", default="config/video_workers.json", help="Config workers cho Flow")
    p.add_argument("--row-start", type=int, default=2, help="Dòng bắt đầu xử lý")
    p.add_argument("--row-end", type=int, default=0, help="Dòng kết thúc, 0 = không giới hạn")
    p.add_argument("--no-public-link", dest="public_link", action="store_false", default=True, help="Tắt quyền xem công khai Drive output (mặc định: luôn public)")
    p.add_argument("--use-proxy", action="store_true", help="Bật proxy theo video_workers.json")
    p.add_argument("--scene-timeout-per-prompt-sec", type=int, default=180, help="Timeout mỗi prompt scene (giây)")
    p.add_argument("--scenario-timeout-sec", type=int, default=30 * 60, help="Timeout tối đa cho 1 row/kịch bản (giây)")
    p.add_argument("--scene-min-success-images", type=int, default=120, help="Mục tiêu mềm số ảnh (để retry)")
    p.add_argument("--scene-retry-failed-rounds", type=int, default=2, help="Số vòng retry prompt fail/timeout")
    # ChichBong Imagen4 engine
    p.add_argument(
        "--generator",
        choices=["flow", "imagen4", "auto"],
        default="imagen4",
        help="Engine tạo ảnh: imagen4=ChichBong (mặc định), flow=Google Flow, auto=xen kẽ 2 engine",
    )
    p.add_argument("--imagen4-license-key", default="", help="License key ChichBong Imagen4 (để trống = tự dò từ ENV/chichbongtaoanh)")
    p.add_argument("--imagen4-aspect-ratio", default="square", choices=["square", "landscape", "portrait"], help="Tỉ lệ ảnh cho Imagen4")
    p.add_argument("--imagen4-quality", default="2k", choices=["1k", "2k", "4k"], help="Chất lượng ảnh Imagen4 (mặc định: 2k)")
    p.add_argument("--imagen4-max-in-flight", type=int, default=15, help="Số prompt chạy đồng thời cho Imagen4 (mặc định: 15)")
    p.add_argument("--imagen4-legacy", action="store_true", help="Dùng model legacy IMAGEN_3_5 cho Imagen4")
    p.add_argument("--imagen4-seed", type=int, default=0, help="Seed cố định cho Imagen4 (0 = random)")
    p.add_argument("--imagen4-image-model-name", default="", help="Model name Imagen4 tuỳ chọn (khi không bật --imagen4-legacy)")
    p.add_argument("--row-stride", type=int, default=1, help="Bước nhảy hàng (dùng khi chạy song song 2 tiến tr��nh)")
    p.add_argument("--row-offset", type=int, default=0, help="Offset trong stride: 0=hàng lẻ, 1=hàng chẵn")
    p.add_argument("--row-ids", default="", help="Danh sách hàng cụ thể cần xử lý, cách nhau dấu phẩy: '2,5,8,11' (do coordinator phân công)")
    p.add_argument("--no-fresh-run", action="store_true", help="Tắt fresh_run: giữ ảnh local cũ để resume/tái dùng (mặc định: luôn chạy mới)")
    p.add_argument("--keep-local", action="store_true", help="Giữ lại file local sau khi upload Drive (mặc định: tự xóa sau khi hoàn tất)")
    return p.parse_args()


def parse_sources_file(path: Path) -> list[dict]:
    """
    Parse file sources thành list item:
      {"sheet": "...", "mode": "black-auto" | "hoa"}
    """
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy file sources: {path}")

    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        sheet = line
        mode = "black-auto"
        if "|" in line:
            left, right = line.split("|", 1)
            sheet = left.strip()
            mode = (right.strip() or "black-auto").lower()
        if not sheet:
            continue
        out.append({"sheet": sheet, "mode": mode})
    return out


def pick_drive_parent_id(mode: str, args: argparse.Namespace) -> str:
    """
    Chọn Drive parent folder ID theo project_mode.
    """
    m = str(mode or "black-auto").strip().lower()
    if m == "hoa":
        return str(args.drive_parent_hoa or "root").strip() or "root"
    return str(args.drive_parent_black_auto or "root").strip() or "root"


def main() -> None:
    args = parse_args()
    items = parse_sources_file(Path(args.sources))
    if not items:
        raise RuntimeError("File sources không có dòng hợp lệ để chạy.")

    summary: list[dict] = []
    total_ok = 0
    total_fail = 0
    no_access_sheets: list[str] = []

    print(f"[multi-sheet] Tổng sheet cần chạy: {len(items)}")

    for idx, item in enumerate(items, start=1):
        sheet = item["sheet"]
        mode = item["mode"]
        drive_parent_id = pick_drive_parent_id(mode, args)
        print("\n" + "=" * 80)
        print(f"[multi-sheet] [{idx}/{len(items)}] mode={mode} | sheet={sheet}")
        print("=" * 80)

        cfg = SheetFlowConfig(
            sheet=sheet,
            credentials=str(args.credentials),
            token_file=str(args.token_file),
            drive_output_parent_id=drive_parent_id,
            workspace_dir=str(args.workspace_dir),
            video_workers_config=str(args.video_workers_config),
            use_proxy=bool(args.use_proxy),
            public_link=bool(args.public_link),
            row_start=int(args.row_start),
            row_end=int(args.row_end),
            scene_timeout_per_prompt_sec=int(args.scene_timeout_per_prompt_sec),
            scenario_timeout_sec=int(args.scenario_timeout_sec),
            scene_min_success_images=int(args.scene_min_success_images),
            scene_retry_failed_rounds=int(args.scene_retry_failed_rounds),
            generator=str(args.generator),
            imagen4_license_key=str(args.imagen4_license_key),
            imagen4_aspect_ratio=str(args.imagen4_aspect_ratio),
            imagen4_quality=str(args.imagen4_quality),
            imagen4_max_in_flight=max(1, min(15, int(args.imagen4_max_in_flight))),
            imagen4_use_legacy_model=bool(args.imagen4_legacy),
            imagen4_seed=int(args.imagen4_seed),
            imagen4_image_model_name=str(args.imagen4_image_model_name),
            row_stride=int(args.row_stride),
            row_offset=int(args.row_offset),
            row_ids=[int(x) for x in args.row_ids.split(",") if x.strip().isdigit()] if args.row_ids else None,
            fresh_run=not bool(args.no_fresh_run),
            delete_local_after_done=not bool(args.keep_local),
        )

        try:
            result = run_sheet_drive_flow_pipeline(cfg)
        except Exception as exc:
            if "403" in str(exc):
                print(f"[multi-sheet] ⛔ Sheet không có quyền truy cập (403) — bỏ qua: {sheet}")
                no_access_sheets.append(_clean_sheet_url(sheet))
                continue
            raise
        summary.append(result)
        total_ok += int(result.get("ok", 0) or 0)
        total_fail += int(result.get("fail", 0) or 0)
        print(f"[multi-sheet] Kết quả sheet: ok={result.get('ok', 0)} | fail={result.get('fail', 0)}")

    out = {
        "sheets": len(items),
        "total_ok": total_ok,
        "total_fail": total_fail,
        "results": summary,
    }
    out_path = Path(args.workspace_dir) / "multi_sheet_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 80)
    print(f"[multi-sheet] DONE | total_ok={total_ok} | total_fail={total_fail}")
    print(f"[multi-sheet] Summary: {out_path}")
    if no_access_sheets:
        print(f"\n{'═'*80}")
        print(f"  CẦN MỞ QUYỀN CHO {len(no_access_sheets)} SHEET SAU:")
        print(f"{'─'*80}")
        for s in no_access_sheets:
            print(s)
        print(f"{'═'*80}")
    else:
        print("=" * 80)


if __name__ == "__main__":
    main()
