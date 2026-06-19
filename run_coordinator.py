#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_coordinator.py
══════════════════
Bước 1: Quét tất cả sheet → tìm hàng chưa có output
Bước 2: Phân công xen kẽ cho 2 engine (Flow và Imagen4)
Bước 3: Chạy cả 2 worker song song ngay lập tức

Ưu điểm so với run_dual_engines.sh:
- Google Sheets API chỉ bị gọi 1 lần mỗi sheet (không bị rate limit 429)
- Cả 2 engine khởi động gần như đồng thời
- Biết trước chính xác bao nhiêu hàng cần xử lý trước khi bắt đầu
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Import Google services từ project hiện tại ────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from services.sheet_drive_flow_service import (
    _build_google_services,
    _extract_sheet_id,
    _find_col,
    _find_header_row,
    _read_sheet_rows_with_hidden_links,
    _is_drive_link_or_id,
)


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_sheet_url(sheet_url: str) -> str:
    sheet_id = _extract_sheet_id(sheet_url)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def scan_pending_rows(
    sheets_svc,
    sheet_url: str,
    col_prompt: str = "Prompt tạo ảnh",
    col_output: str = "Folder ảnh",
    row_start: int = 2,
    row_end: int = 0,
) -> list[int] | None:
    """
    Đọc 1 sheet, trả về danh sách số hàng chưa có output VÀ có link Drive hợp lệ.
    Trả về None nếu sheet bị lỗi 403 (không có quyền).
    """
    sheet_id = _extract_sheet_id(sheet_url)
    try:
        meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        tab_name = meta["sheets"][0]["properties"]["title"]
        rows, hidden_links = _read_sheet_rows_with_hidden_links(
            sheets_svc=sheets_svc,
            spreadsheet_id=sheet_id,
            tab_name=tab_name,
            columns="A:AZ",
            max_rows=2000,
        )
    except Exception as exc:
        exc_str = str(exc)
        if "403" in exc_str or "forbidden" in exc_str.lower():
            print(f"[coordinator] ⛔ Sheet {sheet_id}: không có quyền truy cập (403) — bỏ qua.")
            return None  # sentinel: phân biệt với sheet rỗng hợp lệ
        print(f"[coordinator] ⚠️  Không đọc được sheet {sheet_id}: {exc}")
        return []

    if not rows:
        return []

    header_idx = _find_header_row(rows, col_prompt)
    header = rows[header_idx]
    c_prompt = _find_col(header, col_prompt)
    c_output = _find_col(header, col_output)
    if c_prompt is None or c_output is None:
        print(f"[coordinator] ⚠️  Sheet {sheet_id}: không tìm thấy cột '{col_prompt}' hoặc '{col_output}'")
        return []

    pending: list[int] = []
    for r_idx in range(header_idx + 1, len(rows)):
        row_number = r_idx + 1
        if row_number < max(1, row_start):
            continue
        if row_end > 0 and row_number > row_end:
            break
        row = rows[r_idx]
        prompt_text = row[c_prompt].strip() if c_prompt < len(row) else ""
        prompt_hidden = hidden_links.get((r_idx, c_prompt), "").strip()
        prompt_link = prompt_hidden or prompt_text
        existing_output = row[c_output].strip() if c_output < len(row) else ""

        if existing_output:
            continue  # đã có output → bỏ qua
        if not prompt_link or not _is_drive_link_or_id(prompt_link):
            continue  # không có link Drive hợp lệ → bỏ qua

        pending.append(row_number)

    return pending


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER LAUNCHER
# ═══════════════════════════════════════════════════════════════════════════════

def build_worker_cmd(
    sheet: str,
    mode: str,
    row_ids: list[int],
    args: argparse.Namespace,
    generator: str,
    drive_parent_id: str,
) -> list[str]:
    if not row_ids:
        return []
    ids_str = ",".join(str(r) for r in row_ids)
    cmd = [
        sys.executable, "run_multi_sheet_flow.py",
        "--sources", "/dev/stdin",  # không dùng sources file, truyền sheet trực tiếp
        "--credentials", args.credentials,
        "--token-file", args.token_file,
        "--drive-parent-black-auto", drive_parent_id,
        "--drive-parent-hoa", drive_parent_id,
        "--generator", generator,
        "--row-ids", ids_str,
        "--scene-timeout-per-prompt-sec", str(args.scene_timeout),
        "--scenario-timeout-sec", str(args.scenario_timeout),
    ]
    if generator in ("imagen4", "auto"):
        cmd += ["--imagen4-license-key", args.imagen4_license_key]
        cmd += ["--imagen4-aspect-ratio", args.imagen4_aspect_ratio]
        cmd += ["--imagen4-quality", args.imagen4_quality]
        cmd += ["--imagen4-max-in-flight", str(args.imagen4_max_in_flight)]
        if args.imagen4_legacy:
            cmd += ["--imagen4-legacy"]
        if int(args.imagen4_seed) > 0:
            cmd += ["--imagen4-seed", str(args.imagen4_seed)]
        if str(args.imagen4_image_model_name or "").strip():
            cmd += ["--imagen4-image-model-name", str(args.imagen4_image_model_name).strip()]
    if args.use_proxy:
        cmd += ["--use-proxy"]
    return cmd


def run_worker(label: str, sheet: str, mode: str, row_ids: list[int],
               args: argparse.Namespace, generator: str, drive_parent_id: str,
               log_path: Path) -> int:
    """Chạy 1 worker process, stream output ra console + file log."""
    if not row_ids:
        print(f"[{label}] Không có hàng nào để xử lý.")
        return 0

    ids_str = ",".join(str(r) for r in row_ids)

    # Tạo file sources tạm chỉ chứa 1 sheet
    tmp_sources = log_path.parent / f"tmp_sources_{label}_{int(time.time())}.txt"
    tmp_sources.write_text(f"{sheet}|{mode}\n", encoding="utf-8")

    cmd = [
        sys.executable, "run_multi_sheet_flow.py",
        "--sources", str(tmp_sources),
        "--credentials", args.credentials,
        "--token-file", args.token_file,
        "--drive-parent-black-auto", drive_parent_id,
        "--drive-parent-hoa", drive_parent_id,
        "--generator", generator,
        "--row-ids", ids_str,
        "--scene-timeout-per-prompt-sec", str(args.scene_timeout),
        "--scenario-timeout-sec", str(args.scenario_timeout),
    ]
    if generator in ("imagen4", "auto"):
        cmd += ["--imagen4-license-key", args.imagen4_license_key]
        cmd += ["--imagen4-aspect-ratio", args.imagen4_aspect_ratio]
        cmd += ["--imagen4-quality", args.imagen4_quality]
        cmd += ["--imagen4-max-in-flight", str(args.imagen4_max_in_flight)]
        if args.imagen4_legacy:
            cmd += ["--imagen4-legacy"]
        if int(args.imagen4_seed) > 0:
            cmd += ["--imagen4-seed", str(args.imagen4_seed)]
        if str(args.imagen4_image_model_name or "").strip():
            cmd += ["--imagen4-image-model-name", str(args.imagen4_image_model_name).strip()]
    if args.use_proxy:
        cmd += ["--use-proxy"]

    print(f"[{label}] Khởi động: {len(row_ids)} hàng | rows={ids_str[:80]}{'...' if len(ids_str) > 80 else ''}")

    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8"
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            print(f"[{label}] {line}")
            logf.write(line + "\n")
            logf.flush()
        proc.wait()

    tmp_sources.unlink(missing_ok=True)
    return proc.returncode


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Coordinator: quét sheet rồi chạy song song Flow + Imagen4")
    p.add_argument("--sources", default="sheet_sources.txt")
    p.add_argument("--credentials", default="credentials.json")
    p.add_argument("--token-file", default="token.json")
    p.add_argument("--drive-parent-black-auto", default="root")
    p.add_argument("--drive-parent-hoa", default="root")
    p.add_argument("--imagen4-license-key", default="")
    p.add_argument("--imagen4-aspect-ratio", default="square", choices=["square", "landscape", "portrait"])
    p.add_argument("--imagen4-quality", default="2k", choices=["1k", "2k", "4k"])
    p.add_argument("--imagen4-max-in-flight", type=int, default=15)
    p.add_argument("--imagen4-legacy", action="store_true")
    p.add_argument("--imagen4-seed", type=int, default=0)
    p.add_argument("--imagen4-image-model-name", default="")
    p.add_argument("--row-start", type=int, default=2)
    p.add_argument("--row-end", type=int, default=0)
    p.add_argument("--scene-timeout", type=int, default=180)
    p.add_argument("--scenario-timeout", type=int, default=1800)
    p.add_argument("--use-proxy", action="store_true")
    p.add_argument("--log-dir", default="logs/coordinator")
    return p.parse_args()


def pick_drive_parent(mode: str, args: argparse.Namespace) -> str:
    return args.drive_parent_hoa if mode.strip().lower() == "hoa" else args.drive_parent_black_auto


def parse_sources_file(path: Path) -> list[dict]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sheet, mode = (line.split("|", 1) + ["black-auto"])[:2]
        if sheet.strip():
            out.append({"sheet": sheet.strip(), "mode": mode.strip().lower() or "black-auto"})
    return out


def main() -> None:
    args = parse_args()
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    sources_path = Path(args.sources)
    if not sources_path.exists():
        sys.exit(f"[coordinator] Không tìm thấy file sources: {sources_path}")

    items = parse_sources_file(sources_path)
    if not items:
        sys.exit("[coordinator] File sources không có sheet hợp lệ.")

    # ── Bước 1: Auth Google 1 lần duy nhất ───────────────────────────────────
    print(f"\n{'═'*64}")
    print(f"  COORDINATOR — {len(items)} sheet cần quét")
    print(f"{'═'*64}")
    print("[coordinator] Đang xác thực Google API...")
    sheets_svc, _ = _build_google_services(Path(args.credentials), Path(args.token_file))
    print("[coordinator] Auth thành công.\n")

    # ── Bước 2: Quét tất cả sheet, thu thập hàng pending ─────────────────────
    # Cấu trúc: [{sheet, mode, drive_parent, flow_rows, imagen4_rows}]
    plan: list[dict] = []
    total_flow = total_imagen4 = 0
    no_access_sheets: list[str] = []

    for idx, item in enumerate(items, 1):
        sheet = item["sheet"]
        mode = item["mode"]
        drive_parent = pick_drive_parent(mode, args)

        print(f"[coordinator] [{idx}/{len(items)}] Quét: {sheet[:80]}")
        pending = scan_pending_rows(
            sheets_svc=sheets_svc,
            sheet_url=sheet,
            row_start=args.row_start,
            row_end=args.row_end,
        )

        if pending is None:  # 403 — không có quyền
            no_access_sheets.append(_clean_sheet_url(sheet))
            continue

        # Chia xen kẽ: flow ← hàng ở vị trí 0,2,4...; imagen4 ← vị trí 1,3,5...
        flow_rows = pending[0::2]
        imagen4_rows = pending[1::2]
        total_flow += len(flow_rows)
        total_imagen4 += len(imagen4_rows)

        plan.append({
            "sheet": sheet, "mode": mode, "drive_parent": drive_parent,
            "flow_rows": flow_rows, "imagen4_rows": imagen4_rows,
        })
        print(f"           ✓ Pending={len(pending)} | Flow={len(flow_rows)} | Imagen4={len(imagen4_rows)}")

        # Nghỉ nhẹ giữa các lần đọc sheet để tránh rate limit
        if idx < len(items):
            time.sleep(1.2)

    if no_access_sheets:
        print(f"\n{'═'*64}")
        print(f"  CẦN MỞ QUYỀN CHO {len(no_access_sheets)} SHEET SAU:")
        print(f"{'─'*64}")
        for s in no_access_sheets:
            print(s)
        print(f"{'═'*64}")

    print(f"\n[coordinator] Tổng: Flow={total_flow} hàng | Imagen4={total_imagen4} hàng")

    if total_flow == 0 and total_imagen4 == 0:
        print("[coordinator] Không có hàng nào cần xử lý. Thoát.")
        return

    # Lưu plan để debug
    plan_file = log_dir / f"plan_{timestamp}.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[coordinator] Plan lưu tại: {plan_file}\n")

    # ── Bước 3: Chạy song song 2 engine ──────────────────────────────────────
    print(f"{'═'*64}")
    print("  Khởi động 2 engine song song...")
    print(f"{'═'*64}\n")

    results: dict[str, list] = {"flow": [], "imagen4": []}
    threads: list[threading.Thread] = []

    def run_all_sheets(engine: str, row_key: str, label: str, log_suffix: str) -> None:
        log_path = log_dir / f"{log_suffix}_{timestamp}.log"
        for item in plan:
            row_ids = item[row_key]
            if not row_ids:
                continue
            exit_code = run_worker(
                label=label,
                sheet=item["sheet"],
                mode=item["mode"],
                row_ids=row_ids,
                args=args,
                generator=engine,
                drive_parent_id=item["drive_parent"],
                log_path=log_path,
            )
            results[engine].append({"sheet": item["sheet"], "rows": len(row_ids), "exit": exit_code})

    t_flow = threading.Thread(
        target=run_all_sheets,
        args=("flow", "flow_rows", "FLOW", "flow"),
        daemon=True,
    )
    t_imagen4 = threading.Thread(
        target=run_all_sheets,
        args=("imagen4", "imagen4_rows", "IMG4", "imagen4"),
        daemon=True,
    )

    t_flow.start()
    t_imagen4.start()

    try:
        t_flow.join()
        t_imagen4.join()
    except KeyboardInterrupt:
        print("\n[coordinator] Nhận Ctrl+C, dừng...")
        sys.exit(1)

    # ── Tổng kết ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*64}")
    print("  HOÀN TẤT")
    flow_ok = sum(1 for r in results["flow"] if r["exit"] == 0)
    img4_ok = sum(1 for r in results["imagen4"] if r["exit"] == 0)
    print(f"  Flow    : {flow_ok}/{len(results['flow'])} sheet OK | log: logs/coordinator/flow_{timestamp}.log")
    print(f"  Imagen4 : {img4_ok}/{len(results['imagen4'])} sheet OK | log: logs/coordinator/imagen4_{timestamp}.log")
    print(f"{'═'*64}\n")


if __name__ == "__main__":
    main()
