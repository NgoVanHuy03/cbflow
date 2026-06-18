from __future__ import annotations

import asyncio
import json
import os
import random  # Dùng để tạo khoảng nghỉ ngẫu nhiên giữa các prompt
import re
import time
from collections import defaultdict
from typing import Any

from models.image_job import ImageJob
from services.flow_humanize_service import (
    handle_unusual_activity_with_cooldown,
    has_policy_violation_ui_error,
    sleep_humanized,
)
from services.flow_image_service import (
    download_flow_images,
    run_flow_image_capture,
)
from services.flow_prompt_service import (
    click_flow_generate_button,
    send_flow_prompt,
    type_flow_prompt,
)
from services.prompt_media_map_service import (
    build_prompt_media_batch,
    collect_media_ids_from_obj,
    normalize_media_url,
)


def _looks_like_api_url(url: str) -> bool:
    """
    Check nhanh URL có phải API liên quan đến generate image không.
    """
    u = str(url or "").lower()
    if not u:
        return False
    return (
        "flowmedia:batchgenerateimages" in u
        or "media.getmediaurlredirect" in u
        or "/api/trpc/" in u
    )


def _extract_prompt_index_from_post_data(post_data: str, prompts: list[str]) -> int:
    """
    Map request body -> prompt_index bằng cách tìm prompt text trong post_data.
    """
    body = str(post_data or "")
    if not body:
        return 0
    for idx, prompt_text in enumerate(prompts, start=1):
        p = str(prompt_text or "").strip()
        if p and p in body:
            return idx
    return 0


def _collect_ids_and_urls(obj: Any, out_urls: set[str]) -> None:
    """
    Duyệt JSON response để gom các URL ảnh.
    """
    if isinstance(obj, dict):
        for _k, v in obj.items():
            if isinstance(v, str):
                low = v.lower()
                if low.startswith("http://") or low.startswith("https://"):
                    out_urls.add(v)
            _collect_ids_and_urls(v, out_urls)
    elif isinstance(obj, list):
        for x in obj:
            _collect_ids_and_urls(x, out_urls)


def _media_id_to_redirect_url(media_id: str) -> str:
    """
    Chuẩn hóa media_id thành URL redirect để tải ảnh qua session hiện tại.
    """
    mid = str(media_id or "").strip()
    if not mid:
        return ""
    return f"https://labs.google/fx/api/trpc/media.getMediaUrlRedirect?name={mid}"


def _ordered_scene_filename(prompt_index: int) -> str:
    """
    Tên file chuẩn theo thứ tự prompt để dễ sort: canh_001.png, canh_002.png, ...
    """
    return f"canh_{int(prompt_index):03d}.png"


def _promote_batch_file_to_ordered_name(
    generated_files: list[str],
    output_dir: str,
    prompt_index: int,
) -> str:
    """
    Đổi tên file ảnh đã tải của 1 prompt về chuẩn tuần tự canh_XXX.png.
    """
    if not generated_files:
        return ""
    src = str(generated_files[0] or "").strip()
    if not src:
        return ""
    dst = os.path.join(str(output_dir or "").strip(), _ordered_scene_filename(prompt_index))
    try:
        if os.path.abspath(src) != os.path.abspath(dst):
            os.replace(src, dst)
        return dst
    except Exception as e:
        print(f"[WARN] [pipeline] Không đổi tên file theo thứ tự cho prompt {prompt_index:02d}: {e}")
        return src


async def generate_scene_images_with_pipeline(
    page,
    job: ImageJob,
    timeout_per_prompt_sec: int = 180,
    expected_images_per_prompt: int = 1,
    max_in_flight: int = 2,
    send_gap_sec: float = 1.8,  # Giữ tham số này để tương thích ngược với code cũ
    min_success_images: int = 120,
    scenario_timeout_sec: int = 30 * 60,
    retry_failed_rounds: int = 1,
) -> list[str]:
    """
    Chạy scene prompts theo kiểu pipeline trong 1 tab:
    - Không đợi prompt trước tải xong mới gửi prompt sau.
    - Giữ số prompt "đang bay" theo max_in_flight.
    - Map prompt -> ảnh dựa trên API/media_id.
    - Khoảng nghỉ giữa 2 lần gửi là ngẫu nhiên (lấy từ metadata job nếu có).

    Request gửi đi:
    1) Gửi prompt từng cái với nhịp ngẫu nhiên trong [send_gap_min, send_gap_max].
    2) Song song nghe API response để biết prompt nào đã có media_id.
    3) Prompt nào đạt expected ảnh hoặc timeout sẽ "hoàn tất", nhả slot cho prompt tiếp.

    Response nhận về:
    - Danh sách src ảnh (URL redirect) đã map theo từng prompt.
    - Ảnh tải về lưu theo prefix giống mode tuần tự: <job_id>_001_img1.png, ...
    """
    results: list[str] = []

    prompts = list(job.prompts or [])
    if not prompts:
        print(f"[WARN] Job {job.job_id} không có prompt scene nào để chạy.")
        return results

    meta = job.metadata or {}
    total = len(prompts)
    original_total = int(meta.get("scene_prompt_total", total) or total)
    raw_original_indices = list(meta.get("scene_prompt_indices") or [])
    if len(raw_original_indices) == total:
        prompt_original_indices = [int(x) for x in raw_original_indices]
    else:
        prompt_original_indices = list(range(1, total + 1))

    def _orig_idx(rel_idx: int) -> int:
        if rel_idx <= 0 or rel_idx > total:
            return rel_idx
        return int(prompt_original_indices[rel_idx - 1])

    def _prompt_label(rel_idx: int) -> str:
        return f"{_orig_idx(rel_idx):02d}/{original_total}"

    existing_policy_blocked_abs: set[int] = set()
    for x in list(meta.get("policy_blocked_prompt_indices") or []):
        try:
            v = int(x)
        except Exception:
            continue
        if v > 0:
            existing_policy_blocked_abs.add(v)
    policy_blocked_prompts: set[int] = {
        i for i in range(1, total + 1) if _orig_idx(i) in existing_policy_blocked_abs
    }

    expected = max(1, int(expected_images_per_prompt or 1))
    max_flight = max(1, int(max_in_flight or 1))
    timeout_one = max(20, int(timeout_per_prompt_sec or 180))
    stage_timeout = max(120, int(scenario_timeout_sec or 30 * 60))

    # Target động: prompt bị policy_blocked sẽ không đưa vào mục tiêu thành công.
    def _effective_target() -> int:
        return max(0, total - len(policy_blocked_prompts))

    min_required = _effective_target()
    retry_rounds = max(0, int(retry_failed_rounds or 0))

    # — Đọc khoảng nghỉ ngẫu nhiên từ metadata job (nếu có) —
    # Ưu tiên pipeline_send_gap_min/max (format mới) óị tương thích ngược pipeline_send_gap_sec.
    fast_mode = bool(meta.get("pipeline_fast_mode", False))
    failover_on_unusual = bool(meta.get("pipeline_failover_on_unusual", False))
    if total >= 100:
        safe_cap = int(meta.get("pipeline_high_volume_max_in_flight", 0) or 0)
        if safe_cap > 0 and max_flight > safe_cap:
            print(
                f"[WARN] [pipeline] prompts={total} lớn, tự hạ max_in_flight "
                f"từ {max_flight} -> {safe_cap} để giảm timeout/rate-limit."
            )
            max_flight = safe_cap

    gap_min = float(meta.get("pipeline_send_gap_min") or 0) or max(0.5, send_gap_sec * 0.8)
    gap_max = float(meta.get("pipeline_send_gap_max") or 0) or max(gap_min, send_gap_sec * 1.3)
    burst_size = int(meta.get("pipeline_burst_size", max_flight) or max_flight)
    burst_size = max(1, min(max_flight, burst_size))
    # Khoảng cách giữa các prompt trong cùng 1 burst.
    # Mặc định dùng cùng nhịp gửi chung (vd: 1-2.5s) để tránh "tạo quá nhanh".
    burst_gap_min = float(meta.get("pipeline_burst_gap_min") or gap_min)
    burst_gap_max = float(meta.get("pipeline_burst_gap_max") or gap_max)
    # Clamp để tránh giá trị vô lý
    gap_min = max(0.0, gap_min)
    gap_max = max(gap_min, gap_max)
    burst_gap_min = max(0.0, burst_gap_min)
    burst_gap_max = max(burst_gap_min, burst_gap_max)
    print(
        f"[*] [pipeline] mode=pipeline | prompts={len(prompts)}/{original_total} | "
        f"max_in_flight={max_flight} | burst_size={burst_size} | "
        f"expected={expected} | send_gap=[{gap_min:.2f} - {gap_max:.2f}]s | "
        f"burst_gap=[{burst_gap_min:.2f} - {burst_gap_max:.2f}]s | "
        f"fast_mode={fast_mode} | failover_on_unusual={failover_on_unusual}"
    )
    print(
        f"[*] [pipeline] timeout_per_prompt={timeout_one}s | "
        f"scenario_timeout={stage_timeout}s | min_success={min_required} | "
        f"retry_rounds={retry_rounds} | policy_blocked={len(policy_blocked_prompts)}"
    )

    # --- State tracking ---
    request_meta_by_id: dict[int, dict[str, Any]] = {}
    prompt_to_media_ids: dict[int, set[str]] = defaultdict(set)
    prompt_to_api_urls: dict[int, set[str]] = defaultdict(set)
    prompt_send_ts: dict[int, float] = {}
    prompt_done_reason: dict[int, str] = {}
    network_events: list[dict[str, Any]] = []
    response_tasks: set[asyncio.Task] = set()

    def _on_task_done(t: asyncio.Task) -> None:
        response_tasks.discard(t)

    async def _handle_api_response(response) -> None:
        """
        Parse response API để gom media_id cho từng prompt.
        """
        try:
            req = response.request
            req_id = id(req)
            meta = request_meta_by_id.get(req_id, {})
            prompt_index = int(meta.get("prompt_index", 0) or 0)

            status = int(response.status or 0)
            ct = str((response.headers or {}).get("content-type", "") or "").lower()
            if status >= 500:
                return

            body_json = None
            if "json" in ct or "text" in ct:
                raw = await response.text()
                try:
                    body_json = json.loads(raw)
                except Exception:
                    body_json = None

            if body_json is None:
                return

            media_ids: set[str] = set()
            api_urls: set[str] = set()
            collect_media_ids_from_obj(body_json, media_ids)
            _collect_ids_and_urls(body_json, api_urls)

            if prompt_index > 0:
                for mid in media_ids:
                    prompt_to_media_ids[prompt_index].add(str(mid))
                for u in api_urls:
                    prompt_to_api_urls[prompt_index].add(normalize_media_url(u))

            network_events.append(
                {
                    "type": "api_response",
                    "prompt_index": prompt_index,
                    "status": status,
                    "media_ids_count": len(media_ids),
                    "urls_count": len(api_urls),
                }
            )
        except Exception as e:
            network_events.append({"type": "api_response_parse_error", "error": str(e)})

    def _on_request(req) -> None:
        try:
            if req.resource_type not in {"fetch", "xhr"}:
                return
            url = str(req.url or "")
            if not _looks_like_api_url(url):
                return

            post_data = ""
            try:
                post_data = str(req.post_data or "")
            except Exception:
                post_data = ""

            prompt_index = _extract_prompt_index_from_post_data(post_data, prompts)
            request_meta_by_id[id(req)] = {
                "url": url,
                "method": str(req.method or ""),
                "prompt_index": prompt_index,
            }
            network_events.append(
                {
                    "type": "api_request",
                    "url": url,
                    "method": str(req.method or ""),
                    "prompt_index": prompt_index,
                }
            )
        except Exception:
            pass

    def _on_response(resp) -> None:
        try:
            req = resp.request
            if req.resource_type in {"fetch", "xhr"} and _looks_like_api_url(req.url or ""):
                t = asyncio.create_task(_handle_api_response(resp))
                response_tasks.add(t)
                t.add_done_callback(_on_task_done)
        except Exception:
            pass

    # Đăng ký listeners trước khi gửi prompt để không hụt event đầu tiên.
    page.on("request", _on_request)
    page.on("response", _on_response)

    sent_count = 0
    done_prompts: set[int] = set()
    success_prompt_indices: set[int] = set()
    global_start = time.monotonic()
    global_deadline = global_start + stage_timeout
    soft_timeout_warned = False

    try:
        while len(done_prompts) < total:
            now = time.monotonic()
            if (not soft_timeout_warned) and now >= global_deadline:
                soft_timeout_warned = True
                print(
                    f"[WARN] [pipeline] Đã vượt scenario_timeout={stage_timeout}s. "
                    "Tiếp tục chạy đến khi xử lý hết prompt."
                )

            # 1) Update trạng thái prompt hoàn tất.
            for pidx in range(1, sent_count + 1):
                if pidx in done_prompts:
                    continue
                media_count = len(prompt_to_media_ids.get(pidx, set()))
                age = now - float(prompt_send_ts.get(pidx, now))
                if media_count >= expected:
                    done_prompts.add(pidx)
                    prompt_done_reason[pidx] = f"enough_media_ids({media_count})"
                    print(f"[*] [pipeline] prompt {_prompt_label(pidx)} hoàn tất: đủ ảnh ({media_count}).")
                elif age >= timeout_one:
                    done_prompts.add(pidx)
                    prompt_done_reason[pidx] = f"timeout({int(age)}s)"
                    print(
                        f"[WARN] [pipeline] prompt {_prompt_label(pidx)} timeout {int(age)}s, "
                        f"media_ids={media_count}. Nhả slot để chạy tiếp."
                    )

            # 2) Gửi prompt mới nếu còn slot trống.
            in_flight = sent_count - len(done_prompts)
            burst_sent = 0
            while sent_count < total and in_flight < max_flight:
                idx = sent_count + 1
                if idx in policy_blocked_prompts:
                    sent_count += 1
                    done_prompts.add(idx)
                    prompt_done_reason[idx] = "policy_blocked(previous_run)"
                    in_flight = sent_count - len(done_prompts)
                    print(
                        f"[WARN] [pipeline] prompt {_prompt_label(idx)} đã bị chặn policy từ trước, bỏ qua."
                    )
                    continue

                prompt_text = prompts[idx - 1]
                if fast_mode:
                    await asyncio.sleep(0.03)
                else:
                    await sleep_humanized(0.5, floor=0.2)
                print(f"[*] [pipeline] Gửi prompt {_prompt_label(idx)}...")

                await type_flow_prompt(page, prompt_text, fast=fast_mode)
                sent_ok = await send_flow_prompt(
                    page,
                    refocus_before_send=not fast_mode,
                    fast=fast_mode,
                )
                if not sent_ok:
                    await click_flow_generate_button(page)

                await handle_unusual_activity_with_cooldown(
                    page,
                    stage_label=f"scene_pipeline_prompt_{_orig_idx(idx)}",
                    raise_on_detected=failover_on_unusual,
                )
                policy_blocked_now = await has_policy_violation_ui_error(page)

                prompt_send_ts[idx] = time.monotonic()
                sent_count += 1
                in_flight += 1
                burst_sent += 1
                if policy_blocked_now:
                    policy_blocked_prompts.add(idx)
                    done_prompts.add(idx)
                    prompt_done_reason[idx] = "policy_blocked"
                    in_flight = sent_count - len(done_prompts)
                    print(
                        f"[WARN] [pipeline] prompt {_prompt_label(idx)} bị chặn do policy. "
                        "Không retry prompt này."
                    )

                if sent_count < total:
                    # Gửi theo cụm (burst): trong cùng 1 cụm dùng micro-gap rất ngắn,
                    # chỉ nghỉ dài hơn sau khi đủ 1 burst hoặc đã đầy in_flight.
                    if burst_sent < burst_size and in_flight < max_flight:
                        burst_gap = random.uniform(burst_gap_min, burst_gap_max)
                        print(
                            f"[*] [pipeline] Nghỉ {burst_gap:.2f}s giữa prompt trong burst "
                            f"({burst_sent}/{burst_size})..."
                        )
                        await asyncio.sleep(burst_gap)
                    else:
                        actual_gap = random.uniform(gap_min, gap_max)
                        print(
                            f"[*] [pipeline] Nghỉ {actual_gap:.2f}s sau burst {burst_sent} prompt..."
                        )
                        await asyncio.sleep(actual_gap)
                        burst_sent = 0

            # 3) Nhịp poll ngắn để lấy response mới.
            await asyncio.sleep(1.0)

        # Chờ thêm vài giây để flush response đang bay cuối phiên.
        flush_until = time.monotonic() + 6.0
        while time.monotonic() < flush_until and response_tasks:
            await asyncio.sleep(0.25)

    finally:
        # Gỡ listeners để tránh ảnh hưởng stage khác.
        try:
            page.remove_listener("request", _on_request)
        except Exception:
            pass
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    # --- Download theo prompt map ---
    print("[*] [pipeline] Bắt đầu tải ảnh theo map API/media_id...")
    print(f"[*] [pipeline-map] candidates={total}/{original_total}")
    for idx, prompt_text in enumerate(prompts, start=1):
        orig_idx = _orig_idx(idx)
        if idx in policy_blocked_prompts:
            print(
                f"[WARN] [pipeline-map] prompt {orig_idx:02d}/{original_total} "
                "bị chặn policy -> bỏ qua map/download."
            )
            continue
        media_ids = sorted(prompt_to_media_ids.get(idx, set()))
        mapped_srcs: list[str] = []
        seen_srcs: set[str] = set()

        # Ưu tiên nguồn chắc chắn nhất: media_id từ API response.
        for mid in media_ids:
            u = _media_id_to_redirect_url(mid)
            if u and u not in seen_srcs:
                seen_srcs.add(u)
                mapped_srcs.append(u)

        # Bổ sung URL parse được từ API để tăng tỉ lệ tải thành công.
        # Trường hợp redirect URL theo media_id bị trả lỗi tạm thời thì vẫn còn phương án khác.
        for u in sorted(prompt_to_api_urls.get(idx, set())):
            nu = normalize_media_url(u)
            if nu and nu not in seen_srcs:
                seen_srcs.add(nu)
                mapped_srcs.append(nu)

        # Giới hạn số URL thử cho mỗi prompt để không kéo dài quá lâu.
        # expected thường = 1, ở đây cho thử tối đa 4 URL để tăng độ bền tải.
        max_try_urls = max(1, min(4, len(mapped_srcs)))
        mapped_srcs = mapped_srcs[:max_try_urls]

        prefix = f"{job.job_id}_{orig_idx:03d}"
        saved = 0
        selected_srcs: list[str] = []
        if mapped_srcs:
            # Thử lần lượt từng URL ứng viên, chỉ cần save được 1 ảnh là coi prompt này pass.
            # Cách này xử lý tốt case có media_id nhưng URL đầu tiên lỗi/expire.
            for src_try in mapped_srcs:
                saved = await download_flow_images(
                    page=page,
                    new_srcs=[src_try],
                    output_dir=job.output_dir,
                    prefix=prefix,
                )
                if saved > 0:
                    selected_srcs = [src_try]
                    break

        capture_result = {
            "ok": saved > 0,
            "saved": int(saved),
            "new_srcs": (selected_srcs if saved > 0 else list(mapped_srcs)),
            "baseline_count": 0,
            "download_limit": None,
        }
        batch = build_prompt_media_batch(
            mode="scene_pipeline",
            prompt_index=idx,
            prompt_total=total,
            prompt_text=prompt_text,
            output_dir=job.output_dir,
            prefix=prefix,
            expected_count=expected,
            capture_result=capture_result,
        )
        print(
            f"[*] [pipeline-map] prompt {orig_idx:02d}/{original_total} | "
            f"media_ids={len(media_ids)} srcs={len(batch.new_srcs)} files={len(batch.generated_files)} "
            f"| done_reason={prompt_done_reason.get(idx, 'n/a')}"
        )
        if batch.ok:
            ordered_path = _promote_batch_file_to_ordered_name(
                generated_files=batch.generated_files,
                output_dir=job.output_dir,
                prompt_index=orig_idx,
            )
            if ordered_path:
                print(
                    f"[*] [pipeline-map] prompt {orig_idx:02d} -> file={os.path.basename(ordered_path)}"
                )
            results.extend(batch.new_srcs)
            success_prompt_indices.add(idx)
        else:
            print(f"[ERR] [pipeline] Prompt {orig_idx:02d} không tải được ảnh nào.")

    # --- Retry các prompt fail để đẩy tỷ lệ thành công ---
    if retry_rounds > 0 and len(success_prompt_indices) < _effective_target():
        for retry_no in range(1, retry_rounds + 1):
            if len(success_prompt_indices) >= _effective_target():
                break
            failed_indices = [
                i
                for i in range(1, total + 1)
                if i not in success_prompt_indices and i not in policy_blocked_prompts
            ]
            if not failed_indices:
                break
            print(
                f"[*] [pipeline-retry] Round {retry_no}/{retry_rounds} | "
                f"failed_prompts={len(failed_indices)} | success={len(success_prompt_indices)}/{total} | "
                f"policy_blocked={len(policy_blocked_prompts)}"
            )
            for idx in failed_indices:
                if len(success_prompt_indices) >= _effective_target():
                    break
                timeout_this = timeout_one

                orig_idx = _orig_idx(idx)
                prompt_text = prompts[idx - 1]
                print(
                    f"[*] [pipeline-retry] prompt {orig_idx:02d}/{original_total} | "
                    f"timeout={timeout_this}s"
                )

                await type_flow_prompt(page, prompt_text, fast=fast_mode)
                sent_ok = await send_flow_prompt(
                    page,
                    refocus_before_send=not fast_mode,
                    fast=fast_mode,
                )
                if not sent_ok:
                    await click_flow_generate_button(page)
                await handle_unusual_activity_with_cooldown(
                    page,
                    stage_label=f"scene_pipeline_retry_prompt_{orig_idx}",
                    raise_on_detected=failover_on_unusual,
                )
                if await has_policy_violation_ui_error(page):
                    policy_blocked_prompts.add(idx)
                    prompt_done_reason[idx] = "policy_blocked"
                    print(
                        f"[WARN] [pipeline-retry] Prompt {orig_idx:02d} bị chặn do policy. "
                        "Bỏ qua, không retry tiếp."
                    )
                    continue

                retry_prefix = f"{job.job_id}_{orig_idx:03d}_retry{retry_no}"
                capture_result = await run_flow_image_capture(
                    page=page,
                    output_dir=job.output_dir,
                    prefix=retry_prefix,
                    expected=expected,
                    timeout=timeout_this,
                )
                batch = build_prompt_media_batch(
                    mode="scene_pipeline_retry",
                    prompt_index=idx,
                    prompt_total=total,
                    prompt_text=prompt_text,
                    output_dir=job.output_dir,
                    prefix=retry_prefix,
                    expected_count=expected,
                    capture_result=capture_result,
                )
                if batch.ok:
                    ordered_path = _promote_batch_file_to_ordered_name(
                        generated_files=batch.generated_files,
                        output_dir=job.output_dir,
                        prompt_index=orig_idx,
                    )
                    if ordered_path:
                        print(
                            f"[*] [pipeline-retry] Prompt {orig_idx:02d} -> file={os.path.basename(ordered_path)}"
                        )
                    success_prompt_indices.add(idx)
                    results.extend(batch.new_srcs)
                    print(
                        f"[OK] [pipeline-retry] Prompt {orig_idx:02d} hồi phục thành công "
                        f"(saved={batch.saved_count})."
                    )
                else:
                    print(f"[WARN] [pipeline-retry] Prompt {orig_idx:02d} vẫn chưa có ảnh sau retry.")

    print(
        f"[*] [pipeline-debug] network_events={len(network_events)} | "
        f"sent={sent_count}/{total} | done={len(done_prompts)}/{total} | "
        f"success={len(success_prompt_indices)}/{total} | policy_blocked={len(policy_blocked_prompts)}"
    )

    # Lưu lại danh sách prompt bị policy block (index gốc) để tầng failover không chạy lại.
    blocked_abs = sorted({_orig_idx(i) for i in policy_blocked_prompts} | existing_policy_blocked_abs)
    meta["policy_blocked_prompt_indices"] = blocked_abs
    meta["policy_blocked_count"] = len(blocked_abs)

    target_required = _effective_target()
    if len(success_prompt_indices) < target_required:
        # Chạy theo số round retry cấu hình; nếu chưa đủ thì dùng ảnh hiện có.
        print(
            f"[WARN] [pipeline] Hết retry: "
            f"success={len(success_prompt_indices)}/{total}, target={target_required}, "
            f"policy_blocked={len(policy_blocked_prompts)}. "
            "Tiếp tục dùng ảnh đã có."
        )
    elif policy_blocked_prompts:
        print(
            f"[*] [pipeline] Có {len(policy_blocked_prompts)} prompt bị chặn policy, "
            "đã loại khỏi retry."
        )
    return results
