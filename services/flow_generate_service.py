from typing import List

from models.image_job import ImageJob
from services.flow_reference_generate_service import prepare_reference_images_for_job
from services.flow_scene_generate_service import generate_scene_images_from_job
from services.flow_settings_service import apply_flow_generation_settings_panel


async def generate_images_from_job(
    page,
    job: ImageJob,
    timeout_per_prompt_sec: int = 120,
) -> List[str]:
    """
    Orchestrator tổng cho 1 job tạo ảnh.

    Luồng chạy rõ ràng:
    1) Áp settings trên panel Flow (nếu bật auto_apply).
    2) Chạy stage reference (kiểm tra ảnh tham chiếu đầu vào).
    3) Chạy stage scene image (gửi prompts + chờ render + tải ảnh).

    Tách như vậy giúp:
    - Dễ debug: lỗi nằm ở stage nào nhìn ra ngay.
    - Dễ mở rộng: sau này thêm "tạo reference tự động" không đụng stage scene.
    """
    # 1) Setup panel settings trước khi generate.
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

    # 2) Stage reference: chỉ kiểm tra/tóm tắt reference input ở phiên bản hiện tại.
    reference_summary = await prepare_reference_images_for_job(
        page,
        job,
        timeout_per_ref_sec=min(timeout_per_prompt_sec, 150),
        max_retry_per_reference=3,
    )
    if reference_summary.get("generated_paths"):
        print(
            "[*] Stage reference đã tạo mới "
            f"{len(reference_summary.get('generated_paths', []))} ảnh nhân vật."
        )
    if reference_summary.get("missing_paths"):
        # Yêu cầu vận hành:
        # - Retry reference 3 lần.
        # - Nếu vẫn thiếu thì dừng hẳn job để tránh chạy scene với dữ liệu không đủ.
        missing_count = len(reference_summary.get("missing_paths", []) or [])
        raise RuntimeError(
            f"Thiếu {missing_count} ảnh reference sau 3 lần retry. Dừng job, không chạy stage scene."
        )

    # 3) Stage scene: xử lý prompt scene và tải ảnh output.
    return await generate_scene_images_from_job(
        page=page,
        job=job,
        timeout_per_prompt_sec=timeout_per_prompt_sec,
        expected_images_per_prompt=1,  # Có thể nâng cấp map theo multiplier sau.
    )
