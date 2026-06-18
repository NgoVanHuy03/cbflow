import asyncio
import os
import json
import shutil
from pathlib import Path
import argparse
import traceback

from models.worker_config import WorkerConfig
from models.image_job import ImageJob
from models.flow_settings import FlowSettings
from services.worker_pool_service import WorkerPool
from services.flow_scene_generate_service import generate_scene_images_from_job
from services.prompt_service import parse_image_prompts_file, SCENARIO_IMAGE_FILE
from services.flow_settings_service import load_flow_ui_settings
from services.flow_settings_service import apply_flow_generation_settings_panel
from services.run_cleanup_service import cleanup_scenario_media_files

def _parse_args():
    """
    Parse tham số dòng lệnh.
    - --no-proxy   : chạy bằng mạng thật, không gán proxy vào worker.
    - --scenario   : chỉ chạy kịch bản chỉ định.
    - --scene-mode : override chế độ chạy (ghi đè lên flow_ui_settings.txt nếu truyền vào).
    Lưu ý: Nếu KHÔNG truyền --scene-mode thì runner sẽ tự đọc từ flow_ui_settings.txt.
      Hỗ trợ nhập:
      + A  -> tự đổi thành kich_ban_A
      + kich_ban_A -> dùng nguyên văn
    """
    parser = argparse.ArgumentParser(description="Runner đơn giản cho luồng tạo ảnh theo kịch bản.")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Không dùng proxy, chạy mạng thật."
    )
    parser.add_argument(
        "--scenario",
        type=str,
        help="Chỉ chạy một kịch bản. Ví dụ: A hoặc kich_ban_A"
    )
    parser.add_argument(
        "--scene-mode",
        type=str,
        choices=["serial", "pipeline"],
        default=None,  # None = lấy từ file config
        help="Override chế độ chạy prompt scene. Nếu bỏ qua, lấy từ flow_ui_settings.txt."
    )
    parser.add_argument(
        "--pipeline-max-inflight",
        type=int,
        default=None,  # None = lấy từ file config
        help="Số prompt scene tối đa đang chạy đồng thời trong mode pipeline."
    )
    parser.add_argument(
        "--pipeline-send-gap-sec",
        type=float,
        default=None,  # None = lấy từ file config
        help="Override khoảng nghỉ (giây) giữa 2 lần gửi prompt liên tiếp."
    )
    return parser.parse_args()


def _normalize_scenario_name(raw_name: str | None) -> str | None:
    """
    Chuẩn hóa tên kịch bản người dùng nhập thành đúng tên folder.
    """
    if not raw_name:
        return None
    name = str(raw_name).strip()
    if not name:
        return None
    if name.startswith("kich_ban_"):
        return name
    return f"kich_ban_{name}"


async def _generate_scene_images_with_ui_settings(page, job: ImageJob):
    """
    Runner scene-only nhưng vẫn áp panel settings trước khi generate scene.

    Request gửi đi:
    1) (Nếu auto_apply=true) click chọn mode/model/ratio/multiplier trên panel.
    2) Gửi prompt scene và tải ảnh output.

    Response nhận về:
    - Danh sách URL ảnh mới của stage scene.
    """
    # Bước này giúp main_runner_no_reference vẫn dùng chuẩn cài đặt từ flow_ui_settings.txt
    # mà không cần đi qua orchestrator có stage reference.
    if job.settings and job.settings.auto_apply:
        await apply_flow_generation_settings_panel(
            page,
            top_mode=job.settings.top_mode,
            secondary_mode=job.settings.secondary_mode,
            aspect_ratio=job.settings.aspect_ratio,
            multiplier=job.settings.multiplier,
            model_name=job.settings.model_name,
            allow_model_alias_fallback=job.settings.allow_model_alias_fallback,
        )
    return await generate_scene_images_from_job(page, job)


async def main(args):
    # 1. Lọc kịch bản
    scenarios_dir = "scenarios"
    valid_folders = [f for f in os.listdir(scenarios_dir) 
                    if os.path.isdir(os.path.join(scenarios_dir, f)) and f.startswith("kich_ban_")]

    # Nếu có chỉ định --scenario thì chỉ giữ lại đúng kịch bản đó.
    selected_scenario = _normalize_scenario_name(args.scenario)
    if selected_scenario:
        valid_folders = [f for f in valid_folders if f == selected_scenario]
        if not valid_folders:
            print(f"Không tìm thấy kịch bản '{selected_scenario}' trong thư mục scenarios/.")
            return
        print(f"[i] Chỉ chạy kịch bản: {selected_scenario}")
    
    if not valid_folders:
        print("Không tìm thấy kịch bản nào trong thư mục scenarios/.")
        return

    # Dọn media cũ mỗi lần bắt đầu chạy để tránh nhầm kết quả.
    scenario_paths = [os.path.join(scenarios_dir, f) for f in valid_folders]
    cleanup_report = cleanup_scenario_media_files(scenario_paths)
    print(
        f"[cleanup] Đã xóa {cleanup_report.get('deleted', 0)} file media cũ "
        f"(lỗi: {cleanup_report.get('failed', 0)})."
    )

    # 2. Đọc cài đặt UI từ file config (bao gồm pipeline settings).
    ui_cfg = load_flow_ui_settings()

    # Thứ tự ưu tiên:
    # 1) CLI args nếu có truyền.
    # 2) flow_ui_settings.txt khi CLI bỏ trống.
    effective_scene_mode = (
        str(args.scene_mode).strip().lower()
        if args.scene_mode is not None
        else str(ui_cfg.get("scene_execution_mode", "serial") or "serial").strip().lower()
    )
    effective_max_in_flight = (
        max(1, int(args.pipeline_max_inflight))
        if args.pipeline_max_inflight is not None
        else max(1, int(ui_cfg.get("pipeline_max_in_flight", 2) or 2))
    )
    if args.pipeline_send_gap_sec is not None:
        gap_val = max(0.5, float(args.pipeline_send_gap_sec))
        effective_gap_min = gap_val
        effective_gap_max = gap_val
    else:
        effective_gap_min = float(ui_cfg.get("pipeline_send_gap_min", 1.5) or 1.5)
        effective_gap_max = float(ui_cfg.get("pipeline_send_gap_max", 3.5) or 3.5)

    if effective_scene_mode == "pipeline":
        print(
            f"[pipeline] mode=pipeline | max_in_flight={effective_max_in_flight} | "
            f"send_gap=[{effective_gap_min:.1f} - {effective_gap_max:.1f}]s"
        )
    else:
        print("[pipeline] mode=serial (chạy tuần tự)")

    # 3. Xây dựng danh sách ImageJobs
    jobs = []
    # Map đầy đủ FlowSettings từ flow_ui_settings.txt để apply panel đúng chuẩn.
    default_settings = FlowSettings(
        auto_apply=bool(ui_cfg.get("auto_apply", True)),
        top_mode=str(ui_cfg.get("top_mode", "image") or "image"),
        secondary_mode=str(ui_cfg.get("secondary_mode", "") or ""),
        aspect_ratio=str(ui_cfg.get("aspect_ratio", "16:9") or "16:9"),
        multiplier=str(ui_cfg.get("multiplier", "x1") or "x1"),
        model_name=str(ui_cfg.get("model_name", "Nano Banana 2") or "Nano Banana 2"),
        allow_model_alias_fallback=bool(ui_cfg.get("allow_model_alias_fallback", False)),
    )
    print(
        "[panel] "
        f"auto_apply={default_settings.auto_apply} | "
        f"top_mode={default_settings.top_mode} | "
        f"secondary_mode={default_settings.secondary_mode or '(none)'} | "
        f"aspect_ratio={default_settings.aspect_ratio} | "
        f"multiplier={default_settings.multiplier} | "
        f"model={default_settings.model_name}"
    )

    for folder in valid_folders:
        folder_path = os.path.join(scenarios_dir, folder)
        img_file = os.path.join(folder_path, SCENARIO_IMAGE_FILE)

        if not os.path.exists(img_file):
            print(f"Bỏ qua {folder}: Thiếu file prompt_image.txt")
            continue

        # Load prompts
        img_prompts = parse_image_prompts_file(img_file)

        job = ImageJob(
            job_id=folder,
            prompts=img_prompts,
            # Runner no-reference: luôn bỏ qua stage ảnh nhân vật.
            # Để None nhằm đảm bảo không có reference input được đưa vào pipeline.
            reference_images=None,
            output_dir=os.path.join(folder_path, "output"),
            settings=default_settings,
            # Metadata dành cho stage scene.
            metadata={
                "scenario_dir": folder_path,
                # Metadata điều khiển mode chạy stage scene.
                # serial   : prompt 1 xong mới prompt 2.
                # pipeline : gửi theo cửa sổ in-flight, map theo API/media_id.
                "scene_execution_mode": effective_scene_mode,
                "pipeline_max_in_flight": effective_max_in_flight,
                # Truyền khoảng min/max để pipeline service random đúng cấu hình file.
                "pipeline_send_gap_min": effective_gap_min,
                "pipeline_send_gap_max": effective_gap_max,
                # Giữ key cũ để tương thích ngược nếu service khác còn đọc key này.
                "pipeline_send_gap_sec": (effective_gap_min + effective_gap_max) / 2,
                # Ép timeout cho từng prompt scene (yêu cầu: 180s).
                "scene_timeout_per_prompt_sec": 180,
                # Timeout tối đa cho toàn bộ 1 kịch bản (yêu cầu: 30 phút).
                "scenario_timeout_sec": 30 * 60,
                # Mục tiêu mềm số ảnh mong muốn cho mỗi kịch bản.
                # Hết 30 phút sẽ chốt số ảnh hiện có, không ném lỗi vì chưa đạt target.
                "scene_min_success_images": 120,
                # Retry lại prompt lỗi/timeout sau vòng pipeline chính.
                "scene_retry_failed_rounds": 2,
            },
        )
        jobs.append(job)

    if not jobs:
        print("Không có Job nào hợp lệ.")
        return

    # 3. Đọc Config Workers
    with open("config/video_workers.json", "r") as f:
        cfg_data = json.load(f)

    # Ưu tiên key "video_workers" (config hiện tại), fallback "workers" (config cũ).
    # Mục tiêu: không bắt người dùng phải chỉnh lại file config khi đổi phiên bản.
    worker_rows = cfg_data.get("video_workers") or cfg_data.get("workers") or []
    if not worker_rows:
        print("Không tìm thấy worker nào trong config/video_workers.json.")
        return

    # Nếu chỉ chạy 1 kịch bản, ưu tiên worker có scenario_dir khớp.
    # Ví dụ: --scenario A -> worker có scenario_dir = scenarios/kich_ban_A
    if selected_scenario:
        matched_workers = []
        for w in worker_rows:
            scenario_dir = str(w.get("scenario_dir", "") or "")
            scenario_name = os.path.basename(scenario_dir)
            if scenario_name == selected_scenario:
                matched_workers.append(w)
        if matched_workers:
            worker_rows = matched_workers
            print(f"[i] Tự chọn worker theo scenario: {matched_workers[0].get('worker_id', 'unknown')}")

    workers = []
    # Cắt số lượng Worker vừa đủ với số lượng kịch bản (nếu số kịch bản ít hơn số worker)
    max_workers_needed = min(len(jobs), len(worker_rows))

    use_proxy = not args.no_proxy
    if not use_proxy:
        print("[!] Chế độ KHÔNG dùng Proxy đã được bật (--no-proxy). Đang dùng mạng thật.")

    for w in worker_rows[:max_workers_needed]:
        workers.append(WorkerConfig(
            worker_id=w["worker_id"],
            profile_dir=w["profile_dir"],
            proxy=w.get("proxy") if use_proxy else None
        ))

    if not workers:
        print("Không tạo được worker nào để chạy job.")
        return

    # 4. Bật Pool và uỷ quyền thực thi
    pool = WorkerPool(configs=workers)
    try:
        await pool.start_all()
        # Chạy scene-only nhưng vẫn apply panel settings trước mỗi job.
        await pool.run_jobs_parallel(jobs, _generate_scene_images_with_ui_settings)
    finally:
        await pool.stop_all()
        print("Tất cả đã hoàn tất!")

if __name__ == "__main__":
    cli_args = _parse_args()
    try:
        asyncio.run(main(cli_args))
    except KeyboardInterrupt:
        # Người dùng chủ động Ctrl+C thì thoát êm, không in traceback gây hiểu nhầm lỗi logic.
        print("\n[STOP] Đã dừng theo yêu cầu người dùng (Ctrl+C).")
    except Exception as exc:
        # In chi tiết traceback để dễ bắt lỗi khi chạy từ terminal.
        print(f"[FATAL] Runner bị lỗi chưa xử lý: {exc}")
        traceback.print_exc()
