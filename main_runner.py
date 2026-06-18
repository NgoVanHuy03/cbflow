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
from services.flow_generate_service import generate_images_from_job
from services.prompt_service import parse_character_file, parse_image_prompts_file, SCENARIO_CHARACTER_FILE, SCENARIO_IMAGE_FILE
# Đọc pipeline config từ file cài đặt dùng chung
from services.flow_settings_service import load_flow_ui_settings
from services.run_cleanup_service import cleanup_scenario_media_files

def _parse_args():
    """
    Parse tham số dòng lệnh.
    - --no-proxy   : chạy bằng mạng thật, không gán proxy vào worker.
    - --scenario   : chỉ chạy kịch bản chỉ định.
    - --scene-mode : override chế độ chạy (ghi đè lên flow_ui_settings.txt nếu truyền vào).
    Lưu ý: Nếu KHÔNG truyền --scene-mode thì rúner sẽ tự đọc từ flow_ui_settings.txt.
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
        default=None,  # None = lấy từ config file
        help="Override chế độ chạy prompt scene. Nếu bỏ qua, lấy từ flow_ui_settings.txt."
    )
    parser.add_argument(
        "--pipeline-max-inflight",
        type=int,
        default=None,
        help="Số prompt scene tối đa đang chạy đồng thời trong mode pipeline."
    )
    parser.add_argument(
        "--pipeline-send-gap-sec",
        type=float,
        default=None,
        help="Khoảng nghỉ (giây) giữa 2 lần gửi prompt liên tiếp trong mode pipeline."
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

    # 2. Đọc cài đặt UI từ file config (bao gồm pipeline settings mới)
    ui_cfg = load_flow_ui_settings()

    # Giải quyết thứ tự ưu tiên:
    #   1. CLI args tường minh (--scene-mode, --pipeline-max-inflight) → ưu tiên nhất
    #   2. flow_ui_settings.txt → mặc định khi không truyền CLI
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
    # Khoảng nghỉ ngẫu nhiên: lấy min/max từ config.
    # Được lưu riêng pipeline_send_gap_min và pipeline_send_gap_max trong ui_cfg.
    effective_gap_min = float(ui_cfg.get("pipeline_send_gap_min", 1.5) or 1.5)
    effective_gap_max = float(ui_cfg.get("pipeline_send_gap_max", 3.5) or 3.5)

    # In tóm tắt cấu hình pipeline hiệu dụng để debug nhanh.
    if effective_scene_mode == "pipeline":
        print(
            f"[pipeline] mode=pipeline | max_in_flight={effective_max_in_flight} | "
            f"send_gap=[{effective_gap_min:.1f} - {effective_gap_max:.1f}]s"
        )
    else:
        print(f"[pipeline] mode=serial (chạy tuần tự)")

    # 3. Xây dựng danh sách ImageJobs
    jobs = []
    # Setting (Dùng tạm mặc định 16:9, sau này bạn update có thể dùng hàm đọc file UI)
    default_settings = FlowSettings(aspect_ratio="16:9", model_name="Nano Banana Pro")

    for folder in valid_folders:
        folder_path = os.path.join(scenarios_dir, folder)
        char_file = os.path.join(folder_path, SCENARIO_CHARACTER_FILE)
        img_file = os.path.join(folder_path, SCENARIO_IMAGE_FILE)

        if not os.path.exists(img_file):
            print(f"Bỏ qua {folder}: Thiếu file prompt_image.txt")
            continue

        # Load prompts
        img_prompts = parse_image_prompts_file(img_file)
        
        # Load reference images (nếu kịch bản có ref)
        reference_list = []
        char_data = {}
        if os.path.exists(char_file):
            char_data = parse_character_file(char_file)
            # Ở phần bản hoàn thiện, file tạo ra sẽ là dạng: folder_path/char_XXX.png
            # Hiện tại cứ truyền một list dummy để đánh dấu là "CÓ REF" để engine nhận biết
            for char_id in char_data.keys():
                # Đường dẫn ảo tượng trưng, engine sẽ biết có nhân vật
                reference_list.append(os.path.join(folder_path, f"{char_id}.png"))

        job = ImageJob(
            job_id=folder,
            prompts=img_prompts,
            reference_images=reference_list if reference_list else None,
            output_dir=os.path.join(folder_path, "output"),
            settings=default_settings,
            # Metadata điều khiển mode chạy stage scene.
            # pipeline_send_gap_min / max được truyền xuống để flow_scene_generate_service
            # và flow_prompt_pipeline_service biết khoảng nghỉ ngẫu nhiên.
            metadata={
                "scenario_dir": folder_path,
                "character_prompts": char_data,
                # Chế độ thực thi: "serial" hoặc "pipeline"
                "scene_execution_mode": effective_scene_mode,
                # Số prompt tối đa gửi đồng thời
                "pipeline_max_in_flight": effective_max_in_flight,
                # Khoảng nghỉ ngẫu nhiên (min/max riêng để pipeline service tự random)
                "pipeline_send_gap_min": effective_gap_min,
                "pipeline_send_gap_max": effective_gap_max,
                # Giữ lại key cũ để các service cũ không bị vỡ interface
                "pipeline_send_gap_sec": (effective_gap_min + effective_gap_max) / 2,
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
        await pool.run_jobs_parallel(jobs, generate_images_from_job)
    finally:
        await pool.stop_all()
        print("Tất cả đã hoàn tất!")

if __name__ == "__main__":
    cli_args = _parse_args()
    # Phần in tóm tắt được in bên trong hàm main() sau khi đã giải quyết thứ tự ưu tiên
    try:
        asyncio.run(main(cli_args))
    except Exception as exc:
        # In chi tiết traceback để dễ bắt lỗi khi chạy từ terminal.
        print(f"[FATAL] Runner bị lỗi chưa xử lý: {exc}")
        traceback.print_exc()
