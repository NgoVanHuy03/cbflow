from __future__ import annotations

import os
import shutil
from typing import Any

from models.image_job import ImageJob
from services.flow_image_service import run_flow_image_capture
from services.flow_humanize_service import (
    handle_unusual_activity_with_cooldown,
    sleep_humanized,
)
from services.prompt_media_map_service import build_prompt_media_batch, pick_primary_generated_file
from services.flow_prompt_service import (
    click_flow_generate_button,
    find_and_focus_flow_prompt,
    send_flow_prompt,
    type_flow_prompt,
)


def _extract_char_id_from_path(path: str) -> str:
    """
    Tách mã nhân vật từ path đích.
    Ví dụ: scenarios/kich_ban_A/CHAR01_ABC.png -> CHAR01_ABC
    """
    base = os.path.basename(path or "")
    stem, _ext = os.path.splitext(base)
    return stem.strip()


async def _generate_single_reference_image(
    page,
    char_id: str,
    char_prompt: str,
    target_path: str,
    timeout_per_ref_sec: int,
) -> bool:
    """
    Tạo 1 ảnh reference cho 1 nhân vật và lưu về đúng target_path.

    Request gửi đi:
    1) Focus ô prompt.
    2) Paste prompt nhân vật.
    3) Enter (hoặc fallback click Generate).
    4) Chờ render và tải ảnh mới.

    Response nhận về:
    - Nếu capture thành công -> copy 1 file thành <CHAR_ID>.png.
    - Nếu capture fail -> trả False để caller log và xử lý tiếp.
    """
    scenario_dir = os.path.dirname(target_path)
    os.makedirs(scenario_dir, exist_ok=True)

    # Nghỉ ngắn trước khi thao tác để tránh pattern quá đều.
    await sleep_humanized(0.8, floor=0.25)

    print(f"[*] [ref] Tạo ảnh nhân vật: {char_id}")
    await find_and_focus_flow_prompt(page)
    await type_flow_prompt(page, char_prompt)
    sent_ok = await send_flow_prompt(page)
    if not sent_ok:
        await click_flow_generate_button(page)

    # Nếu dính cảnh báo unusual ở bước reference thì cooldown/reload nhẹ.
    await handle_unusual_activity_with_cooldown(
        page,
        stage_label=f"reference_{char_id}",
    )

    capture_result = await run_flow_image_capture(
        page=page,
        output_dir=scenario_dir,
        prefix=char_id,
        expected=1,
        timeout=timeout_per_ref_sec,
        # Reference chỉ cần 1 ảnh đại diện, không cần tải toàn bộ multiplier.
        max_download=1,
    )

    # Map chung theo prompt_media_map_service để reference và scene dùng cùng rule.
    batch = build_prompt_media_batch(
        mode="reference",
        prompt_index=1,
        prompt_total=1,
        prompt_text=char_prompt,
        output_dir=scenario_dir,
        prefix=char_id,
        expected_count=1,
        capture_result=capture_result,
    )
    print(
        f"[*] [ref-map] {char_id} -> srcs={len(batch.new_srcs)} files={len(batch.generated_files)}"
    )

    if not batch.ok:
        print(f"[WARN] [ref] Không capture được ảnh cho {char_id}.")
        return False

    latest = pick_primary_generated_file(batch)
    if not latest or not os.path.exists(latest):
        print(f"[WARN] [ref] Không tìm thấy file ảnh hợp lệ cho {char_id}.")
        return False

    # Dùng copy2 để giữ bản gốc <prefix>_img*.png cho debug, đồng thời tạo file chuẩn <CHAR_ID>.png.
    shutil.copy2(latest, target_path)
    print(f"[OK] [ref] Đã lưu reference: {target_path}")
    await sleep_humanized(1.0, floor=0.3)
    return True


async def prepare_reference_images_for_job(
    page,
    job: ImageJob,
    timeout_per_ref_sec: int = 120,
    max_retry_per_reference: int = 3,
) -> dict[str, Any]:
    """
    Stage xử lý ảnh tham chiếu (reference) cho 1 job.

    Mục tiêu:
    - Tách riêng khỏi flow tạo ảnh scene để code dễ đọc, dễ debug.
    - Kiểm tra file reference nào đang có / đang thiếu.
    - Nếu thiếu và có prompt nhân vật trong metadata -> tự generate trước.
    - Retry tạo lại ảnh reference khi fail (mặc định 3 lần).
    - Trả về summary để tầng orchestrator có thể log rõ ràng.
    """
    character_prompts = (job.metadata or {}).get("character_prompts", {}) or {}

    valid_paths: list[str] = []
    missing_paths: list[str] = []
    generated_paths: list[str] = []
    failed_generate_paths: list[str] = []

    # Nếu job không có danh sách reference thì trả summary rỗng.
    if not job.reference_images:
        return {
            "ok": True,
            "has_reference_input": False,
            "valid_paths": valid_paths,
            "missing_paths": missing_paths,
        }

    # Quét từng đường dẫn reference để phân loại có/không.
    for ref_img_path in job.reference_images:
        if ref_img_path and os.path.exists(ref_img_path):
            valid_paths.append(ref_img_path)
        else:
            missing_paths.append(ref_img_path)
            print(f"[WARN] Reference image not found: {ref_img_path}")

    # Số lần retry tối đa cho từng ảnh reference.
    # Bảo vệ tối thiểu = 1 để không bị vòng lặp rỗng nếu ai đó truyền 0.
    max_retry = max(1, int(max_retry_per_reference or 1))

    # Nếu thiếu file reference thì thử tự generate theo prompt nhân vật.
    for missing_path in list(missing_paths):
        char_id = _extract_char_id_from_path(missing_path)
        char_prompt = str(character_prompts.get(char_id, "") or "").strip()
        if not char_prompt:
            print(f"[WARN] [ref] Không có prompt_character cho '{char_id}', bỏ qua auto-generate.")
            failed_generate_paths.append(missing_path)
            continue

        # Retry theo yêu cầu: thử tối đa 3 lần (hoặc giá trị caller truyền vào).
        # Mỗi lần đều tạo lại trực tiếp trên Flow rồi kiểm tra file đầu ra đã có chưa.
        ok = False
        for attempt in range(1, max_retry + 1):
            if attempt > 1:
                print(f"[WARN] [ref] Retry {attempt}/{max_retry} cho {char_id}...")
            ok = await _generate_single_reference_image(
                page=page,
                char_id=char_id,
                char_prompt=char_prompt,
                target_path=missing_path,
                timeout_per_ref_sec=timeout_per_ref_sec,
            )
            if ok and os.path.exists(missing_path):
                break

        if ok and os.path.exists(missing_path):
            generated_paths.append(missing_path)
            valid_paths.append(missing_path)
            missing_paths.remove(missing_path)
        else:
            failed_generate_paths.append(missing_path)

    # In summary giúp đọc log nhanh ở terminal.
    print(
        "[*] Reference summary: "
        f"found={len(valid_paths)} | generated={len(generated_paths)} | missing={len(missing_paths)}"
    )

    return {
        # Quy ước:
        # - ok=True khi tất cả reference đã sẵn sàng.
        # - ok=False khi còn thiếu bất kỳ reference nào sau retry.
        "ok": len(missing_paths) == 0,
        "has_reference_input": True,
        "valid_paths": valid_paths,
        "missing_paths": missing_paths,
        "generated_paths": generated_paths,
        "failed_generate_paths": failed_generate_paths,
    }
