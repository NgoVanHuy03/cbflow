from __future__ import annotations

from typing import List

from models.image_job import ImageJob
from services.flow_prompt_service import (
    click_flow_generate_button,
    find_and_focus_flow_prompt,
    send_flow_prompt,
    type_flow_prompt,
)
from services.flow_image_service import run_flow_image_capture
from services.flow_humanize_service import (
    handle_unusual_activity_with_cooldown,
    sleep_humanized,
)
from services.prompt_media_map_service import build_prompt_media_batch
from services.flow_prompt_pipeline_service import generate_scene_images_with_pipeline


async def generate_scene_images_from_job(
    page,
    job: ImageJob,
    timeout_per_prompt_sec: int = 180,
    expected_images_per_prompt: int = 1,
) -> List[str]:
    """
    Stage generate ảnh scene (image prompts) cho 1 job.

    Request chính của stage này:
    1. Focus prompt box.
    2. Gõ prompt scene.
    3. Gửi prompt (Enter hoặc fallback bấm nút Generate).
    4. Chờ render + tải ảnh về output_dir.

    Response của stage này:
    - Danh sách URL ảnh mới (`new_srcs`) thu được từ mỗi prompt.
    - Danh sách này dùng để debug nhanh prompt nào có ảnh, prompt nào fail.
    """
    results: List[str] = []

    # Không có prompt thì trả rỗng luôn.
    if not job.prompts:
        print(f"[WARN] Job {job.job_id} không có prompt scene nào để chạy.")
        return results

    # Cho phép chọn mode chạy scene:
    # - serial   : chạy tuần tự truyền thống (ổn định cao).
    # - pipeline : gửi prompt theo cửa sổ in-flight để tiết kiệm thời gian.
    scene_mode = str((job.metadata or {}).get("scene_execution_mode", "serial") or "serial").strip().lower()
    pipeline_max_in_flight = int((job.metadata or {}).get("pipeline_max_in_flight", 2) or 2)
    pipeline_send_gap_sec = float((job.metadata or {}).get("pipeline_send_gap_sec", 1.8) or 1.8)
    per_prompt_timeout = int((job.metadata or {}).get("scene_timeout_per_prompt_sec", timeout_per_prompt_sec) or timeout_per_prompt_sec)
    scenario_timeout_sec = int((job.metadata or {}).get("scenario_timeout_sec", 30 * 60) or (30 * 60))
    min_success_images = int((job.metadata or {}).get("scene_min_success_images", 120) or 120)
    retry_failed_rounds = int((job.metadata or {}).get("scene_retry_failed_rounds", 1) or 1)
    if scene_mode == "pipeline":
        return await generate_scene_images_with_pipeline(
            page=page,
            job=job,
            timeout_per_prompt_sec=per_prompt_timeout,
            expected_images_per_prompt=expected_images_per_prompt,
            max_in_flight=max(1, pipeline_max_in_flight),
            send_gap_sec=max(0.2, pipeline_send_gap_sec),
            min_success_images=min_success_images,
            scenario_timeout_sec=scenario_timeout_sec,
            retry_failed_rounds=retry_failed_rounds,
        )

    # Chạy tuần tự từng prompt để kiểm soát mapping prompt -> output rõ ràng.
    for i, prompt in enumerate(job.prompts):
        # Nhịp nghỉ ngắn trước mỗi prompt để tránh pattern đều.
        await sleep_humanized(0.9, floor=0.25)
        print(f"[*] Gửi prompt ({i + 1}/{len(job.prompts)}) cho job {job.job_id}...")

        # 1) Focus ô prompt để tránh paste nhầm vị trí.
        await find_and_focus_flow_prompt(page)

        # 2) Gõ nội dung prompt scene.
        await type_flow_prompt(page, prompt)

        # 3) Gửi prompt: ưu tiên Enter, fallback nút Generate.
        sent_ok = await send_flow_prompt(page)
        if not sent_ok:
            await click_flow_generate_button(page)

        # Nếu bị cảnh báo unusual activity thì cooldown/reload trước khi chờ ảnh.
        await handle_unusual_activity_with_cooldown(
            page,
            stage_label=f"scene_prompt_{i + 1}",
        )

        # 4) Chờ render + tải ảnh cho prompt hiện tại.
        prefix = f"{job.job_id}_{i + 1:03d}"
        capture_result = await run_flow_image_capture(
            page=page,
            output_dir=job.output_dir,
            prefix=prefix,
            expected=expected_images_per_prompt,
            timeout=per_prompt_timeout,
        )

        # Map chung: gom toàn bộ output của prompt hiện tại thành 1 batch.
        batch = build_prompt_media_batch(
            mode="scene",
            prompt_index=i + 1,
            prompt_total=len(job.prompts),
            prompt_text=prompt,
            output_dir=job.output_dir,
            prefix=prefix,
            expected_count=expected_images_per_prompt,
            capture_result=capture_result,
        )
        print(
            f"[*] [scene-map] prompt {batch.prompt_index}/{batch.prompt_total} "
            f"-> srcs={len(batch.new_srcs)} files={len(batch.generated_files)}"
        )

        # Hàm capture trả dict trạng thái; chỉ ghi nhận khi ok=True.
        if batch.ok:
            results.extend(batch.new_srcs)
        else:
            print(f"[ERR] Không tải được ảnh nào cho prompt: {prompt}")

        # Nhịp nghỉ ngắn sau mỗi prompt để tránh gửi dồn cứng nhịp.
        await sleep_humanized(1.2, floor=0.35)

    return results
