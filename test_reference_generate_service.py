#!/usr/bin/env python3
"""
Live test chuyên cho stage tạo ảnh nhân vật tham chiếu (reference).

Mục tiêu:
- Chạy tương tự pipeline của main_runner:
  1) Đọc scenario trong thư mục scenarios/
  2) Đọc worker trong config/video_workers.json
  3) Dùng WorkerPool mở Chrome profile thật + vào project Flow mới
- Chỉ chạy stage reference (KHÔNG chạy scene prompt_image).
- In DEBUG panel dạng JSON dễ đọc: request gửi gì, response nhận gì, kiểm tra pass/fail.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from models.image_job import ImageJob
from models.worker_config import WorkerConfig
from services.flow_reference_generate_service import prepare_reference_images_for_job
from services.prompt_service import parse_character_file, SCENARIO_CHARACTER_FILE
from services.worker_pool_service import WorkerPool


def _parse_args() -> argparse.Namespace:
    """
    Parse tham số CLI cho script test.

    Flags chính:
    - --scenario: chạy riêng 1 kịch bản (A hoặc kich_ban_A).
    - --no-proxy: bỏ proxy, chạy mạng thật.
    - --timeout-per-ref: timeout tối đa cho mỗi ảnh nhân vật.
    - --worker: ép chạy 1 worker cụ thể (mặc định video_1).
    """
    parser = argparse.ArgumentParser(
        description="Live test stage reference: tạo ảnh nhân vật tham chiếu và tải về"
    )
    parser.add_argument("--scenario", type=str, default=None, help="A hoặc kich_ban_A")
    parser.add_argument("--no-proxy", action="store_true", help="Không dùng proxy")
    parser.add_argument("--timeout-per-ref", type=int, default=120, help="Timeout mỗi ảnh reference (giây)")
    parser.add_argument("--worker", type=str, default="video_1", help="worker_id ưu tiên khi test")
    parser.add_argument(
        "--debug-max-rows",
        type=int,
        default=12,
        help="Số dòng preview tối đa trong DEBUG panel",
    )
    return parser.parse_args()


def _normalize_scenario_name(raw_name: str | None) -> str | None:
    """
    Chuẩn hóa tên kịch bản:
    - A -> kich_ban_A
    - kich_ban_A -> giữ nguyên
    """
    if not raw_name:
        return None
    s = str(raw_name).strip()
    if not s:
        return None
    if s.startswith("kich_ban_"):
        return s
    return f"kich_ban_{s}"


def _tail_text(value: str, tail_len: int = 140) -> str:
    """
    Cắt ngắn chuỗi dài để panel dễ đọc.
    """
    s = str(value or "")
    if len(s) <= max(1, int(tail_len)):
        return s
    return "..." + s[-max(1, int(tail_len)):]


def _preview_list(values: list[Any], max_items: int) -> list[Any]:
    """
    Lấy mẫu danh sách để tránh log quá dài.
    """
    rows = list(values or [])
    if len(rows) <= max(1, int(max_items)):
        return rows
    keep = max(1, int(max_items))
    return rows[:keep] + [f"...(+{len(rows) - keep} items)"]


def _print_debug_panel(title: str, payload: dict) -> None:
    """
    In JSON DEBUG panel dạng dễ đọc.
    """
    print("\n" + "=" * 72)
    print(f"DEBUG PANEL | {title}")
    print("=" * 72)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("=" * 72)


def _collect_scenarios(selected_scenario: str | None) -> list[str]:
    """
    Lấy danh sách folder scenario hợp lệ.
    """
    scenarios_dir = "scenarios"
    if not os.path.isdir(scenarios_dir):
        return []
    valid = [
        f
        for f in os.listdir(scenarios_dir)
        if os.path.isdir(os.path.join(scenarios_dir, f)) and f.startswith("kich_ban_")
    ]
    if selected_scenario:
        valid = [f for f in valid if f == selected_scenario]
    return sorted(valid)


def _build_reference_jobs(selected_scenario: str | None) -> list[ImageJob]:
    """
    Xây danh sách ImageJob chỉ phục vụ stage reference.

    Request đầu vào:
    - Đọc prompt_character.txt
    Response:
    - Mỗi nhân vật sinh ra 1 target path dạng scenarios/<kb>/<CHAR_ID>.png
    """
    jobs: list[ImageJob] = []
    for folder in _collect_scenarios(selected_scenario):
        scenario_dir = os.path.join("scenarios", folder)
        char_file = os.path.join(scenario_dir, SCENARIO_CHARACTER_FILE)
        if not os.path.exists(char_file):
            print(f"[WARN] Bỏ qua {folder}: thiếu {SCENARIO_CHARACTER_FILE}")
            continue

        character_prompts = parse_character_file(char_file)
        if not character_prompts:
            print(f"[WARN] Bỏ qua {folder}: không parse được prompt nhân vật.")
            continue

        reference_paths: list[str] = []
        for char_id in character_prompts.keys():
            reference_paths.append(os.path.join(scenario_dir, f"{char_id}.png"))

        jobs.append(
            ImageJob(
                job_id=folder,
                prompts=[],
                output_dir=os.path.join(scenario_dir, "output"),
                reference_images=reference_paths,
                metadata={
                    "scenario_dir": scenario_dir,
                    "character_prompts": character_prompts,
                },
            )
        )
    return jobs


def _load_workers(selected_scenario: str | None, no_proxy: bool, prefer_worker: str) -> list[WorkerConfig]:
    """
    Đọc config worker tương tự main_runner.

    Rule chọn:
    1) Nếu có scenario -> ưu tiên worker có scenario_dir khớp.
    2) Nếu có worker_id đúng --worker -> ưu tiên worker đó.
    3) Nếu không match thì fallback worker đầu tiên.
    """
    cfg_path = Path("config/video_workers.json")
    if not cfg_path.exists():
        raise FileNotFoundError(f"Không tìm thấy {cfg_path}")

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    rows = data.get("video_workers") or data.get("workers") or []
    if not rows:
        raise RuntimeError("Không có worker nào trong config/video_workers.json")

    picked = rows
    if selected_scenario:
        by_scenario = []
        for row in rows:
            scenario_dir = str(row.get("scenario_dir", "") or "")
            if os.path.basename(scenario_dir) == selected_scenario:
                by_scenario.append(row)
        if by_scenario:
            picked = by_scenario
            print(f"[i] Tự chọn worker theo scenario: {by_scenario[0].get('worker_id', 'unknown')}")

    # Ưu tiên worker user yêu cầu nếu tồn tại trong danh sách đang dùng.
    found = [r for r in picked if str(r.get("worker_id", "")) == str(prefer_worker)]
    if found:
        picked = found
    else:
        picked = [picked[0]]
        print(f"[WARN] Không tìm thấy worker '{prefer_worker}' trong nhóm hiện tại, dùng {picked[0].get('worker_id')}")

    workers: list[WorkerConfig] = []
    for row in picked[:1]:
        workers.append(
            WorkerConfig(
                worker_id=str(row["worker_id"]),
                profile_dir=str(row["profile_dir"]),
                proxy=(None if no_proxy else row.get("proxy")),
            )
        )
    return workers


def _path_file_info(path: str) -> dict[str, Any]:
    """
    Trả metadata file để debug dễ đọc.
    """
    p = Path(path)
    exists = p.exists()
    if not exists:
        return {
            "path": path,
            "exists": False,
            "size_kb": 0,
            "mtime": "",
        }
    st = p.stat()
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "path": path,
        "exists": True,
        "size_kb": round(float(st.st_size) / 1024.0, 2),
        "mtime": mtime,
    }


async def main() -> int:
    """
    Entry point chính:
    - Build jobs reference-only.
    - Chạy WorkerPool và gọi stage prepare_reference_images_for_job.
    - Ghi report JSON ra debug_sessions/reference_test_<timestamp>/report.json
    """
    args = _parse_args()
    selected_scenario = _normalize_scenario_name(args.scenario)
    if selected_scenario:
        print(f"[i] Chỉ chạy kịch bản: {selected_scenario}")
    if args.no_proxy:
        print("[!] Chế độ KHÔNG dùng Proxy đã được bật (--no-proxy). Đang dùng mạng thật.")

    jobs = _build_reference_jobs(selected_scenario)
    if not jobs:
        print("Không có job reference hợp lệ để chạy.")
        return 1

    workers = _load_workers(selected_scenario, args.no_proxy, args.worker)
    if not workers:
        print("Không tạo được worker nào.")
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("debug_sessions") / f"reference_test_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Nơi gom kết quả của từng job để in summary cuối và ghi JSON.
    all_results: list[dict[str, Any]] = []

    async def _run_reference_only(page, job: ImageJob) -> list[str]:
        """
        Hàm generate_func truyền vào WorkerPool.run_jobs_parallel.

        Request gửi đi:
        - page: tab Flow đã mở project mới.
        - job: chứa danh sách reference target + prompt nhân vật.

        Response nhận về:
        - summary từ prepare_reference_images_for_job.
        - list path ảnh generated (trả về để WorkerPool log số lượng).
        """
        scenario_dir = str((job.metadata or {}).get("scenario_dir", "") or "")
        character_prompts = dict((job.metadata or {}).get("character_prompts", {}) or {})
        target_paths = list(job.reference_images or [])

        before_map = {p: _path_file_info(p) for p in target_paths}
        req_payload = {
            "job_id": job.job_id,
            "scenario_dir": scenario_dir,
            "worker_requested": workers[0].worker_id if workers else "",
            "timeout_per_ref_sec": max(30, int(args.timeout_per_ref)),
            "characters_count": len(character_prompts),
            "characters": _preview_list(
                [
                    {
                        "char_id": k,
                        "prompt_len": len(str(v or "")),
                        "target_path": os.path.join(scenario_dir, f"{k}.png"),
                    }
                    for k, v in character_prompts.items()
                ],
                max_items=max(3, int(args.debug_max_rows)),
            ),
        }

        summary = await prepare_reference_images_for_job(
            page=page,
            job=job,
            timeout_per_ref_sec=max(30, int(args.timeout_per_ref)),
        )

        generated_paths = list(summary.get("generated_paths", []) or [])
        missing_paths = list(summary.get("missing_paths", []) or [])
        valid_paths = list(summary.get("valid_paths", []) or [])
        failed_generate_paths = list(summary.get("failed_generate_paths", []) or [])

        after_map = {p: _path_file_info(p) for p in target_paths}
        changed_files = []
        for p in target_paths:
            before_mtime = str((before_map.get(p) or {}).get("mtime", ""))
            after_mtime = str((after_map.get(p) or {}).get("mtime", ""))
            if before_mtime != after_mtime:
                changed_files.append(p)

        # Request/Response/checks đúng format debug panel dễ đọc.
        panel = {
            "request": req_payload,
            "response": {
                "summary_ok": bool(summary.get("ok")),
                "has_reference_input": bool(summary.get("has_reference_input")),
                "generated_count": len(generated_paths),
                "missing_count": len(missing_paths),
                "valid_count": len(valid_paths),
                "failed_generate_count": len(failed_generate_paths),
                "generated_paths_preview": [_tail_text(x) for x in _preview_list(generated_paths, int(args.debug_max_rows))],
                "missing_paths_preview": [_tail_text(x) for x in _preview_list(missing_paths, int(args.debug_max_rows))],
                "changed_files_preview": [_tail_text(x) for x in _preview_list(changed_files, int(args.debug_max_rows))],
                "files_after_preview": _preview_list([after_map[p] for p in target_paths], int(args.debug_max_rows)),
            },
            "checks": {
                "all_targets_exist_after": all(bool((after_map.get(p) or {}).get("exists")) for p in target_paths),
                "missing_is_zero": len(missing_paths) == 0,
                "status": "PASS" if (len(missing_paths) == 0 and bool(valid_paths)) else "WARN",
            },
        }

        _print_debug_panel(title=f"reference_job_{job.job_id}", payload=panel)

        all_results.append(
            {
                "job_id": job.job_id,
                "request": req_payload,
                "summary": summary,
                "before_files": before_map,
                "after_files": after_map,
                "checks": panel["checks"],
            }
        )
        # WorkerPool chỉ cần list để log "ra được X ảnh".
        return generated_paths

    pool = WorkerPool(configs=workers)
    try:
        await pool.start_all()
        await pool.run_jobs_parallel(jobs, _run_reference_only)
    finally:
        await pool.stop_all()
        print("Tất cả đã hoàn tất!")

    # Ghi report tổng ra file để bạn mở đọc/lưu lại lịch sử test.
    report = {
        "request": {
            "scenario": selected_scenario,
            "jobs_count": len(jobs),
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "profile_dir": w.profile_dir,
                    "proxy": w.proxy,
                }
                for w in workers
            ],
            "timeout_per_ref_sec": max(30, int(args.timeout_per_ref)),
        },
        "response": {
            "results": all_results,
            "result_count": len(all_results),
        },
        "checks": {
            "all_jobs_reported": len(all_results) == len(jobs),
            "all_jobs_pass": all(bool((x.get("checks") or {}).get("status") == "PASS") for x in all_results),
            "status": (
                "PASS"
                if len(all_results) == len(jobs)
                and all(bool((x.get("checks") or {}).get("status") == "PASS") for x in all_results)
                else "WARN"
            ),
        },
    }
    report_path = out_dir / "reference_generate_live_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nKẾT QUẢ TEST REFERENCE")
    print("-" * 72)
    for row in all_results:
        chk = row.get("checks", {}) or {}
        summ = row.get("summary", {}) or {}
        print(
            f"{row.get('job_id')}: status={chk.get('status')} "
            f"generated={len(list(summ.get('generated_paths', []) or []))} "
            f"missing={len(list(summ.get('missing_paths', []) or []))} "
            f"valid={len(list(summ.get('valid_paths', []) or []))}"
        )
    print(f"report json      : {report_path}")
    print("-" * 72)

    if report["checks"]["status"] == "PASS":
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

