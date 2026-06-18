#!/usr/bin/env python3
"""
Live integration test cho prompt_media_map_service bằng Chrome profile thật (worker_1).

Mục tiêu:
- Không mock.
- Mở Chrome thật bằng profile worker_1.
- Tạo project mới trên Google Flow.
- Gửi 1 prompt thật.
- Chờ render và tải ảnh thật.
- Dùng prompt_media_map_service để map kết quả: 1 prompt -> nhiều ảnh.

Cách chạy nhanh (khuyên dùng):
    python3 test_prompt_media_map_service.py --worker video_1 --scenario A

Nếu muốn tự nhập prompt:
    python3 test_prompt_media_map_service.py --worker video_1 --prompt "Portrait photo of a person..."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from models.worker_config import WorkerConfig
from services.flow_image_service import (
    capture_flow_baseline_srcs,
    get_flow_image_srcs,
    download_flow_images,
    run_flow_image_capture,
    scroll_flow_page_to_load_all,
    wait_for_flow_images,
)
from services.flow_prompt_service import (
    click_flow_generate_button,
    find_and_focus_flow_prompt,
    send_flow_prompt,
    type_flow_prompt,
)
from services.prompt_media_map_service import build_prompt_media_batch
from services.worker_pool_service import WorkerPool


def _normalize_scenario_name(raw: str | None) -> str | None:
    """Chuẩn hóa scenario: A -> kich_ban_A."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("kich_ban_"):
        return s
    return f"kich_ban_{s}"


def _load_worker_profile_from_config(worker_id: str) -> tuple[str, str | None]:
    """
    Đọc profile/proxy của worker từ config/video_workers.json.

    Response trả về:
    - profile_dir: đường dẫn profile Chrome của worker
    - proxy: proxy string (nếu có), caller có thể bỏ qua khi --no-proxy
    """
    cfg_path = Path("config/video_workers.json")
    if not cfg_path.exists():
        raise FileNotFoundError(f"Không tìm thấy {cfg_path}")

    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    rows = data.get("video_workers", []) or data.get("workers", []) or []
    for row in rows:
        if str(row.get("worker_id", "")) == str(worker_id):
            profile_dir = str(row.get("profile_dir", "")).strip()
            if not profile_dir:
                raise ValueError(f"worker '{worker_id}' thiếu profile_dir")
            proxy = row.get("proxy")
            return profile_dir, proxy

    raise ValueError(f"Không tìm thấy worker '{worker_id}' trong config/video_workers.json")


def _build_default_prompt_from_scenario(scenario_name: str | None) -> str:
    """
    Prompt mặc định để test nhanh việc map 1 prompt -> nhiều ảnh.

    Gợi ý prompt theo hướng ảnh chân dung để hệ thống dễ tạo output.
    """
    scene_label = scenario_name or "test_live"
    return (
        f"CẢNH TEST {scene_label}: Ảnh chân dung studio, ánh sáng mềm, photorealistic, "
        "một nhân vật đứng chính diện, hậu cảnh đơn giản, độ chi tiết cao, --no text overlay"
    )


def _parse_prompt_list(raw_prompt: str, raw_prompts: list[str] | None, scenario_name: str | None) -> list[str]:
    """
    Chuẩn hóa danh sách prompt để test.

    Ưu tiên:
    1) --prompts "p1" "p2" "p3"
    2) --prompt "single prompt"
    3) mặc định 3 prompt đơn giản để test liên tiếp.
    """
    if raw_prompts:
        cleaned = [str(p).strip() for p in raw_prompts if str(p).strip()]
        if cleaned:
            return cleaned
    if raw_prompt.strip():
        return [raw_prompt.strip()]
    # Mặc định test 3 prompt liên tiếp như bạn yêu cầu.
    return ["con mèo", "con chó", "con ngựa"]


def _build_overlap_report(batches: list[dict]) -> dict:
    """
    Kiểm tra giao nhau URL ảnh giữa các prompt để phát hiện dấu hiệu lẫn.
    """
    overlaps = []
    for i in range(len(batches)):
        for j in range(i + 1, len(batches)):
            left = batches[i]
            right = batches[j]
            left_srcs = set(left.get("new_srcs", []) or [])
            right_srcs = set(right.get("new_srcs", []) or [])
            inter = sorted(left_srcs.intersection(right_srcs))
            overlaps.append(
                {
                    "left_prompt_index": left.get("prompt_index"),
                    "right_prompt_index": right.get("prompt_index"),
                    "overlap_count": len(inter),
                    "overlap_sample": inter[:10],
                }
            )
    total_overlap = sum(int(x.get("overlap_count", 0) or 0) for x in overlaps)
    return {"pairs": overlaps, "total_overlap_count": total_overlap}


def _tail_text(value: str, tail_len: int = 140) -> str:
    """
    Cắt ngắn chuỗi dài để debug panel dễ đọc.

    Lý do:
    - URL ảnh thường rất dài, in full sẽ khó đọc log.
    - Chỉ giữ phần đuôi là đủ để nhận diện media_id/tên file.
    """
    s = str(value or "")
    if len(s) <= max(1, int(tail_len)):
        return s
    return "..." + s[-max(1, int(tail_len)):]


def _preview_list(values: list, max_items: int = 12) -> list:
    """
    Lấy mẫu danh sách để panel gọn, tránh flood terminal.
    """
    rows = list(values or [])
    if len(rows) <= max(1, int(max_items)):
        return rows
    keep = max(1, int(max_items))
    return rows[:keep] + [f"...(+{len(rows) - keep} items)"]


def _print_debug_panel(title: str, payload: dict, enabled: bool = True) -> None:
    """
    In panel DEBUG theo format JSON dễ đọc.

    Cấu trúc chung:
    - request: script đã gửi gì
    - response: script nhận được gì
    - checks: điều kiện pass/fail nhanh
    """
    if not enabled:
        return
    print("\n" + "=" * 72)
    print(f"DEBUG PANEL | {title}")
    print("=" * 72)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("=" * 72)


def _build_single_prompt_debug_panel(
    *,
    prompt_index: int,
    prompt_total: int,
    prompt_text: str,
    expected_count: int,
    prefix: str,
    output_dir: str,
    capture_result: dict,
    batch: dict,
) -> dict:
    """
    Dựng panel cho mode chạy từng prompt.
    """
    new_srcs = list(batch.get("new_srcs", []) or [])
    generated_files = list(batch.get("generated_files", []) or [])
    saved_count = int(batch.get("saved_count", 0) or 0)
    expected = max(1, int(expected_count or 1))
    has_any_output = bool(new_srcs or generated_files or saved_count > 0)
    return {
        "request": {
            "mode": "single_prompt",
            "prompt_index": int(prompt_index),
            "prompt_total": int(prompt_total),
            "prompt_text_preview": str(prompt_text or "")[:180],
            "expected_count": expected,
            "prefix": str(prefix or ""),
            "output_dir": str(output_dir or ""),
            "capture_result_keys": sorted(list((capture_result or {}).keys())),
        },
        "response": {
            "batch_ok": bool(batch.get("ok")),
            "saved_count": saved_count,
            "new_srcs_count": len(new_srcs),
            "generated_files_count": len(generated_files),
            "note": str(batch.get("note", "")),
            "new_srcs_preview": [_tail_text(x) for x in _preview_list(new_srcs)],
            "generated_files_preview": [_tail_text(x) for x in _preview_list(generated_files)],
        },
        "checks": {
            "has_any_output": has_any_output,
            "expected_min_reached": len(new_srcs) >= expected or len(generated_files) >= expected,
            "status": "PASS" if (bool(batch.get("ok")) and has_any_output) else "WARN",
        },
    }


def _build_burst_debug_panel(
    *,
    prompt_list: list[str],
    expected_per_prompt: int,
    new_srcs: list[str],
    api_map_report: dict,
    overlap_report: dict,
    network_events: list[dict],
    max_rows: int,
) -> dict:
    """
    Dựng panel cho mode gửi dồn (burst).
    """
    api_by_prompt = dict((api_map_report or {}).get("by_prompt", {}) or {})
    by_prompt_compare = []
    for pidx in range(1, len(prompt_list) + 1):
        api_rows = list(api_by_prompt.get(pidx, []) or [])
        by_prompt_compare.append(
            {
                "prompt_index": pidx,
                "prompt_text_preview": str(prompt_list[pidx - 1])[:120],
                "api_count": len(api_rows),
                "api_preview": [_tail_text(x) for x in _preview_list(api_rows, max_items=max_rows)],
            }
        )

    unknown_srcs = list((api_map_report or {}).get("unknown_srcs", []) or [])
    unknown_media_ids = list((api_map_report or {}).get("unknown_media_ids", []) or [])
    total_overlap = int((overlap_report or {}).get("total_overlap_count", 0) or 0)
    return {
        "request": {
            "mode": "burst",
            "prompt_count": len(prompt_list),
            "expected_per_prompt": max(1, int(expected_per_prompt or 1)),
            "total_new_srcs": len(list(new_srcs or [])),
        },
        "response": {
            "by_prompt_compare": by_prompt_compare,
            "unknown_srcs_count": len(unknown_srcs),
            "unknown_srcs_preview": [_tail_text(x) for x in _preview_list(unknown_srcs, max_items=max_rows)],
            "unknown_media_ids_count": len(unknown_media_ids),
            "unknown_media_ids_preview": _preview_list(unknown_media_ids, max_items=max_rows),
            "network_event_count": len(list(network_events or [])),
            "network_events_preview": _preview_list(list(network_events or []), max_items=max_rows),
        },
        "checks": {
            "overlap_total": total_overlap,
            "map_api_has_unknown": len(unknown_srcs) > 0 or len(unknown_media_ids) > 0,
            "status": "PASS" if (total_overlap == 0 and not unknown_srcs and not unknown_media_ids) else "WARN",
        },
    }


def _normalize_url(url: str) -> str:
    """
    Chuẩn hóa URL để so khớp dễ hơn.

    Lưu ý đặc biệt:
    - Với media.getMediaUrlRedirect phải GIỮ query `name=...` vì đây là media_id.
    - Nếu bỏ query string toàn bộ thì sẽ mất khả năng map prompt -> media.
    """
    s = str(url or "").strip()
    if not s:
        return ""
    low = s.lower()
    if "media.getmediaurlredirect" in low and "name=" in low:
        try:
            u = urlparse(s)
            q = parse_qs(u.query or "")
            name = (q.get("name", [""])[0] or "").strip()
            if name:
                return f"{u.scheme}://{u.netloc}{u.path}?name={name}"
        except Exception:
            return s
    return s.split("?", 1)[0]


def _extract_media_id_from_redirect_url(url: str) -> str:
    """
    Tách media_id từ URL redirect:
    .../media.getMediaUrlRedirect?name=<media_id>
    """
    s = str(url or "").strip()
    if not s:
        return ""
    if "media.getMediaUrlRedirect" not in s:
        return ""
    try:
        u = urlparse(s)
        q = parse_qs(u.query or "")
        return str((q.get("name", [""])[0] or "")).strip()
    except Exception:
        return ""


def _collect_media_ids_from_obj(obj, out_media_ids: set[str]) -> None:
    """
    Duyệt JSON để gom media_id/mediaId/name có dạng UUID-ish.
    """
    uuid_like = re.compile(r"^[0-9a-fA-F-]{16,}$")
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in {"media_id", "mediaid", "primarymediaid", "name", "id"} and isinstance(v, (str, int)):
                sv = str(v).strip()
                if uuid_like.match(sv):
                    out_media_ids.add(sv)
            _collect_media_ids_from_obj(v, out_media_ids)
    elif isinstance(obj, list):
        for x in obj:
            _collect_media_ids_from_obj(x, out_media_ids)


def _looks_like_api_url(url: str) -> bool:
    """Nhận diện URL API liên quan generate/history của Flow."""
    low = str(url or "").lower()
    hints = [
        "/trpc/",
        "flowmedia:batchgenerateimages",
        "batchgenerateimages",
        "get_history_by_ids",
        "projectinitialdata",
    ]
    return any(h in low for h in hints)


def _collect_ids_and_urls(obj, submit_ids: set, task_ids: set, urls: set) -> None:
    """
    Duyệt đệ quy JSON để gom:
    - submit_id / submitId
    - task_id / job_id / record_id
    - URL ảnh/video (ưu tiên ảnh)
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in {"submit_id", "submitid"} and isinstance(v, (str, int)):
                submit_ids.add(str(v))
            if lk in {"task_id", "taskid", "job_id", "jobid", "record_id", "recordid", "history_record_id"} and isinstance(v, (str, int)):
                task_ids.add(str(v))
            if isinstance(v, str) and v.startswith("http"):
                lv = v.lower()
                if any(ext in lv for ext in [".png", ".jpg", ".jpeg", ".webp", "/media/", "image", "img"]):
                    urls.add(v)
            _collect_ids_and_urls(v, submit_ids, task_ids, urls)
    elif isinstance(obj, list):
        for item in obj:
            _collect_ids_and_urls(item, submit_ids, task_ids, urls)
    elif isinstance(obj, str):
        if obj.startswith("http"):
            lv = obj.lower()
            if any(ext in lv for ext in [".png", ".jpg", ".jpeg", ".webp", "/media/", "image", "img"]):
                urls.add(obj)


def _extract_prompt_index_from_post_data(post_data: str, prompt_list: list[str]) -> int:
    """
    Tìm prompt_index dựa trên nội dung post_data.
    Ưu tiên match prompt dài hơn để tránh match nhầm chuỗi con.
    """
    text = str(post_data or "")
    if not text:
        return 0
    ranked = sorted(
        [(idx + 1, p) for idx, p in enumerate(prompt_list) if str(p).strip()],
        key=lambda x: len(x[1]),
        reverse=True,
    )
    for idx, p in ranked:
        if p in text:
            return idx
    return 0


async def run_live_test(args) -> int:
    """
    Luồng test live chính.

    Request chính script gửi đi:
    1) Mở Chrome worker thật bằng profile persistent.
    2) Đóng tab cũ, mở tab mới, tạo project mới trên Flow.
    3) Gửi prompt test thật.
    4) Chờ render/tải ảnh thật.
    5) Map dữ liệu bằng prompt_media_map_service.

    Response chính script nhận về:
    - Số ảnh mới (new_srcs)
    - Danh sách file đã lưu
    - Batch map đầy đủ theo 1 prompt
    """
    scenario_name = _normalize_scenario_name(args.scenario)
    profile_dir, proxy_from_config = _load_worker_profile_from_config(args.worker)

    prompt_list = _parse_prompt_list(args.prompt, args.prompts, scenario_name)

    # Tạo thư mục output riêng cho lần test live này để không đè dữ liệu cũ.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("debug_sessions") / f"live_prompt_map_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Nếu --no-proxy thì không gắn proxy vào Chrome.
    proxy = None if args.no_proxy else proxy_from_config

    worker_cfg = WorkerConfig(
        worker_id=args.worker,
        profile_dir=profile_dir,
        proxy=proxy,
    )

    pool = WorkerPool(configs=[worker_cfg])
    page = None

    try:
        await pool.start_all()
        context = pool.contexts[args.worker]

        # Dùng chung logic mở tab sạch + tạo project mới từ WorkerPool hiện tại.
        await pool._close_all_existing_tabs(context)
        page = await pool._open_flow_new_project_tab(context, args.worker)

        print("\n" + "=" * 72)
        print("LIVE TEST: prompt_media_map_service (worker thật)")
        print("=" * 72)
        print(f"Worker           : {args.worker}")
        print(f"Profile          : {profile_dir}")
        print(f"Proxy dùng       : {proxy or 'KHÔNG'}")
        print(f"Scenario         : {scenario_name or 'không chỉ định'}")
        print(f"Output dir       : {out_dir}")
        print(f"Số prompt test   : {len(prompt_list)}")
        for idx, ptxt in enumerate(prompt_list, start=1):
            print(f"Prompt {idx:02d}       : {ptxt[:120]}{'...' if len(ptxt) > 120 else ''}")
        print("=" * 72 + "\n")

        all_batches: list[dict] = []
        all_capture_results: list[dict] = []
        debug_panels: list[dict] = []

        # Khởi tạo trước để report cuối luôn có đủ field dù chạy mode nào.
        api_map_report: dict = {}
        src_first_seen_ts: dict[str, float] = {}
        network_events: list[dict] = []
        burst_new_srcs: list[str] = []

        if args.consecutive_send:
            # Mode gửi liên tiếp: prompt sau được gửi sau X giây, không chờ capture từng prompt.
            # Dùng để test hành vi burst giống thao tác "paste liên tục".
            print(
                f"\n[MODE] consecutive_send=ON | send_gap={float(args.send_gap_sec):.2f}s "
                "(gửi liên tiếp rồi mới capture tổng)"
            )
            # QUAN TRỌNG:
            # Chụp baseline TRƯỚC khi gửi prompt để tránh đếm thiếu ảnh.
            # Nếu baseline chụp sau khi đã gửi prompt, ảnh render sớm có thể bị coi là ảnh cũ.
            print("[BURST] Đang chụp baseline trước khi gửi chuỗi prompt...")
            before_srcs = await capture_flow_baseline_srcs(page)
            print(f"[BURST] Baseline trước chuỗi prompt: {len(before_srcs)} ảnh")
            src_first_seen_ts = {}
            all_sent = False
            request_meta_by_id: dict[int, dict] = {}
            network_events = []
            prompt_to_api_urls: dict[int, set[str]] = defaultdict(set)
            prompt_to_submit_ids: dict[int, set[str]] = defaultdict(set)
            prompt_to_task_ids: dict[int, set[str]] = defaultdict(set)
            prompt_to_media_ids: dict[int, set[str]] = defaultdict(set)
            submit_to_prompt: dict[str, int] = {}
            task_to_prompt: dict[str, int] = {}
            media_to_prompt: dict[str, int] = {}

            async def _collect_new_src_timestamps():
                """
                Poll src mới để ghi lại thời điểm xuất hiện đầu tiên của từng ảnh.
                """
                stable_no_new_rounds = 0
                seen_any = False
                start_monotonic = asyncio.get_event_loop().time()
                timeout_sec = max(30, int(args.timeout))
                while True:
                    now = asyncio.get_event_loop().time()
                    if now - start_monotonic > timeout_sec:
                        break
                    current = await get_flow_image_srcs(page)
                    new_srcs = sorted(set(current) - set(before_srcs))
                    new_found = False
                    for src in new_srcs:
                        if src not in src_first_seen_ts:
                            src_first_seen_ts[src] = now
                            new_found = True
                            seen_any = True
                    if new_found:
                        stable_no_new_rounds = 0
                    else:
                        stable_no_new_rounds += 1

                    # Chỉ cho phép dừng sớm khi:
                    # - đã gửi xong
                    # - đã thấy ít nhất 1 ảnh mới
                    # - không có ảnh mới trong ~30s
                    # - và đã đạt gần đủ kỳ vọng (>= 80%)
                    expected_total = max(1, int(args.expected)) * max(1, len(prompt_list))
                    have = len(src_first_seen_ts)
                    if (
                        all_sent
                        and seen_any
                        and stable_no_new_rounds >= 20
                        and have >= int(expected_total * 0.8)
                    ):
                        break
                    await asyncio.sleep(1.5)

            async def _handle_api_response(response):
                """Parse response API để map prompt -> submit/task/url."""
                try:
                    req = response.request
                    req_id = id(req)
                    meta = request_meta_by_id.get(req_id, {})
                    prompt_index = int(meta.get("prompt_index", 0) or 0)
                    url = str(meta.get("url", "") or req.url or "")
                    status = int(response.status or 0)
                    ct = str((response.headers or {}).get("content-type", "") or "").lower()
                    body_text = ""
                    body_json = None
                    if ("json" in ct or "text" in ct) and status < 500:
                        body_text = await response.text()
                        try:
                            body_json = json.loads(body_text)
                        except Exception:
                            body_json = None

                    submit_ids: set[str] = set()
                    task_ids: set[str] = set()
                    urls: set[str] = set()
                    media_ids: set[str] = set()
                    if body_json is not None:
                        _collect_ids_and_urls(body_json, submit_ids, task_ids, urls)
                        _collect_media_ids_from_obj(body_json, media_ids)

                    # Nếu request đã biết prompt_index -> gán trực tiếp.
                    if prompt_index > 0:
                        for sid in submit_ids:
                            submit_to_prompt[sid] = prompt_index
                            prompt_to_submit_ids[prompt_index].add(sid)
                        for tid in task_ids:
                            task_to_prompt[tid] = prompt_index
                            prompt_to_task_ids[prompt_index].add(tid)
                        for mid in media_ids:
                            media_to_prompt[mid] = prompt_index
                            prompt_to_media_ids[prompt_index].add(mid)
                        for u in urls:
                            prompt_to_api_urls[prompt_index].add(_normalize_url(u))

                    # Nếu request chưa match prompt nhưng có submit/task đã biết:
                    # kéo URL về prompt tương ứng.
                    linked_prompts = set()
                    for sid in submit_ids:
                        if sid in submit_to_prompt:
                            linked_prompts.add(int(submit_to_prompt[sid]))
                    for tid in task_ids:
                        if tid in task_to_prompt:
                            linked_prompts.add(int(task_to_prompt[tid]))
                    for mid in media_ids:
                        if mid in media_to_prompt:
                            linked_prompts.add(int(media_to_prompt[mid]))
                    for lp in linked_prompts:
                        for mid in media_ids:
                            prompt_to_media_ids[lp].add(mid)
                        for u in urls:
                            prompt_to_api_urls[lp].add(_normalize_url(u))

                    network_events.append(
                        {
                            "type": "api_response",
                            "url": url,
                            "status": status,
                            "content_type": ct[:80],
                            "prompt_index_from_request": prompt_index,
                            "submit_ids": sorted(list(submit_ids))[:20],
                            "task_ids": sorted(list(task_ids))[:20],
                            "media_ids": sorted(list(media_ids))[:20],
                            "image_urls_count": len(urls),
                        }
                    )
                except Exception as e:
                    network_events.append(
                        {
                            "type": "api_response_parse_error",
                            "error": str(e),
                        }
                    )

            def _on_request(req):
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
                    prompt_index = _extract_prompt_index_from_post_data(post_data, prompt_list)
                    # Nếu request là redirect URL có name=media_id và media_id đã biết prompt
                    # thì gán prompt_index từ media map để debug chính xác hơn.
                    if prompt_index <= 0:
                        mid = _extract_media_id_from_redirect_url(url)
                        if mid and mid in media_to_prompt:
                            prompt_index = int(media_to_prompt[mid])
                    request_meta_by_id[id(req)] = {
                        "ts": time.time(),
                        "url": url,
                        "method": str(req.method or ""),
                        "prompt_index": prompt_index,
                        "post_data_sample": post_data[:1500],
                    }
                    network_events.append(
                        {
                            "type": "api_request",
                            "url": url,
                            "method": str(req.method or ""),
                            "prompt_index": prompt_index,
                            "post_data_sample": post_data[:300],
                        }
                    )
                except Exception:
                    pass

            def _on_response(resp):
                try:
                    req = resp.request
                    if req.resource_type in {"fetch", "xhr"} and _looks_like_api_url(req.url or ""):
                        asyncio.create_task(_handle_api_response(resp))
                except Exception:
                    pass

            # Bật listener network debug cho mode burst.
            page.on("request", _on_request)
            page.on("response", _on_response)
            collector_task = asyncio.create_task(_collect_new_src_timestamps())
            for idx, prompt_text in enumerate(prompt_list, start=1):
                print(f"\n--- SEND PROMPT {idx}/{len(prompt_list)} ---")
                await find_and_focus_flow_prompt(page)
                await type_flow_prompt(page, prompt_text)
                sent_ok = await send_flow_prompt(page)
                if not sent_ok:
                    await click_flow_generate_button(page)
                if idx < len(prompt_list):
                    await asyncio.sleep(max(0.3, float(args.send_gap_sec)))
            all_sent = True

            # Chờ collector kết thúc tự nhiên theo điều kiện ổn định/timeout.
            try:
                await collector_task
            except Exception:
                pass

            # Scroll 1 lượt rồi quét lại để gom thêm ảnh lazy-load (nếu có).
            try:
                await scroll_flow_page_to_load_all(page)
                current_after_scroll = await get_flow_image_srcs(page)
                for src in sorted(set(current_after_scroll) - set(before_srcs)):
                    if src not in src_first_seen_ts:
                        src_first_seen_ts[src] = asyncio.get_event_loop().time()
            except Exception:
                pass

            burst_prefix = f"live_{args.worker}_{ts}_burst"
            burst_expected = max(1, int(args.expected)) * max(1, len(prompt_list))
            print(
                f"[BURST] Chờ ảnh mới theo baseline cũ (expected~{burst_expected}, timeout={max(30, int(args.timeout))}s)..."
            )
            # Ưu tiên danh sách src đã collector ghi nhận để tránh hụt do wait kết thúc sớm.
            new_srcs = sorted(src_first_seen_ts.keys())
            # Nếu collector không có dữ liệu hoặc quá ít, fallback qua wait_for_flow_images.
            if len(new_srcs) < max(1, int(burst_expected * 0.5)):
                waited_srcs = await wait_for_flow_images(
                    page=page,
                    before_srcs=before_srcs,
                    expected=burst_expected,
                    timeout=max(30, int(args.timeout)),
                )
                new_srcs = sorted(set(new_srcs).union(set(waited_srcs or [])))
            burst_new_srcs = list(new_srcs or [])
            saved = await download_flow_images(
                page=page,
                new_srcs=new_srcs,
                output_dir=str(out_dir),
                prefix=burst_prefix,
            )
            capture_result = {
                "ok": saved > 0,
                "saved": int(saved),
                "new_srcs": list(new_srcs or []),
                "baseline_count": len(before_srcs),
            }

            # Dựng map theo API/media_id (nguồn sự thật chính trong mode burst).
            api_map_report = {
                "by_prompt": {},
                "unknown_srcs": [],
                "unknown_media_ids": [],
                "note": "map theo API request/response + media_id-first",
            }
            norm_src_to_full = {_normalize_url(s): s for s in (new_srcs or [])}
            # Map media_id -> full src để join theo redirect name.
            media_id_to_src: dict[str, str] = {}
            for s in (new_srcs or []):
                mid = _extract_media_id_from_redirect_url(s)
                if mid:
                    media_id_to_src[mid] = s
            assigned_norm_srcs = set()
            assigned_media_ids = set()
            for pidx in range(1, len(prompt_list) + 1):
                matched = []
                # Ưu tiên match bằng media_id.
                for mid in sorted(prompt_to_media_ids.get(pidx, set())):
                    if mid in media_id_to_src:
                        matched.append(media_id_to_src[mid])
                        assigned_media_ids.add(mid)
                        assigned_norm_srcs.add(_normalize_url(media_id_to_src[mid]))
                # Fallback match bằng URL normalize.
                api_urls = sorted(prompt_to_api_urls.get(pidx, set()))
                for nu in api_urls:
                    if nu in norm_src_to_full:
                        full = norm_src_to_full[nu]
                        if full not in matched:
                            matched.append(full)
                        assigned_norm_srcs.add(nu)
                api_map_report["by_prompt"][pidx] = matched
            for nu, full in norm_src_to_full.items():
                if nu not in assigned_norm_srcs:
                    api_map_report["unknown_srcs"].append(full)
            for mid in sorted(media_id_to_src.keys()):
                if mid not in assigned_media_ids:
                    api_map_report["unknown_media_ids"].append(mid)

            print("[BURST-MAP] Kết quả map theo API:")
            for pidx in range(1, len(prompt_list) + 1):
                cnt = len((api_map_report.get("by_prompt", {}) or {}).get(pidx, []) or [])
                print(f"  - prompt {pidx:02d}: {cnt} ảnh")
            unknown_cnt = len(api_map_report.get("unknown_srcs", []) or [])
            if unknown_cnt > 0:
                print(f"[WARN] [BURST-MAP] Ảnh chưa map được theo API: {unknown_cnt}")

            burst_batch = build_prompt_media_batch(
                mode="scene_burst",
                prompt_index=1,
                prompt_total=1,
                prompt_text=" | ".join(prompt_list),
                output_dir=str(out_dir),
                prefix=burst_prefix,
                expected_count=burst_expected,
                capture_result=capture_result,
            )
            all_capture_results.append(capture_result or {})
            all_batches.append(burst_batch.to_dict())
            print(
                f"[BATCH BURST] ok={burst_batch.ok} saved={int((capture_result or {}).get('saved', 0) or 0)} "
                f"srcs={len(burst_batch.new_srcs)} files={len(burst_batch.generated_files)}"
            )
        else:
            # Mode mặc định: chạy từng prompt, mỗi prompt capture riêng.
            for idx, prompt_text in enumerate(prompt_list, start=1):
                print(f"\n--- RUN PROMPT {idx}/{len(prompt_list)} ---")
                # 1) Focus ô prompt
                await find_and_focus_flow_prompt(page)

                # 2) Gõ prompt
                await type_flow_prompt(page, prompt_text)

                # 3) Gửi prompt (Enter ưu tiên, fallback nút Generate)
                sent_ok = await send_flow_prompt(page)
                if not sent_ok:
                    await click_flow_generate_button(page)

                # 4) Chờ render và tải ảnh thật
                prefix = f"live_{args.worker}_{ts}_{idx:03d}"
                capture_result = await run_flow_image_capture(
                    page=page,
                    output_dir=str(out_dir),
                    prefix=prefix,
                    expected=max(1, int(args.expected)),
                    timeout=max(30, int(args.timeout)),
                )

                # 5) Map prompt -> nhiều ảnh bằng service chung
                batch = build_prompt_media_batch(
                    mode="scene",
                    prompt_index=idx,
                    prompt_total=len(prompt_list),
                    prompt_text=prompt_text,
                    output_dir=str(out_dir),
                    prefix=prefix,
                    expected_count=max(1, int(args.expected)),
                    capture_result=capture_result,
                )
                all_capture_results.append(capture_result or {})
                all_batches.append(batch.to_dict())

                print(
                    f"[BATCH {idx}] ok={batch.ok} saved={int((capture_result or {}).get('saved', 0) or 0)} "
                    f"srcs={len(batch.new_srcs)} files={len(batch.generated_files)}"
                )
                panel = _build_single_prompt_debug_panel(
                    prompt_index=idx,
                    prompt_total=len(prompt_list),
                    prompt_text=prompt_text,
                    expected_count=max(1, int(args.expected)),
                    prefix=prefix,
                    output_dir=str(out_dir),
                    capture_result=capture_result or {},
                    batch=batch.to_dict(),
                )
                debug_panels.append(panel)
                _print_debug_panel(
                    title=f"single_prompt_{idx:02d}",
                    payload=panel,
                    enabled=bool(args.debug_panel),
                )

        overlap_report = _build_overlap_report(all_batches)
        if args.consecutive_send:
            burst_panel = _build_burst_debug_panel(
                prompt_list=prompt_list,
                expected_per_prompt=max(1, int(args.expected)),
                new_srcs=burst_new_srcs,
                api_map_report=api_map_report,
                overlap_report=overlap_report,
                network_events=network_events,
                max_rows=max(3, int(args.debug_max_rows)),
            )
            debug_panels.append(burst_panel)
            _print_debug_panel(
                title="burst_map_compare",
                payload=burst_panel,
                enabled=bool(args.debug_panel),
            )
        report = {
            "request": {
                "worker": args.worker,
                "profile_dir": profile_dir,
                "proxy": proxy,
                "scenario": scenario_name,
                "prompts": prompt_list,
                "expected": max(1, int(args.expected)),
                "timeout": max(30, int(args.timeout)),
            },
            "response": {
                "capture_results": all_capture_results,
                "batches": all_batches,
                "overlap_report": overlap_report,
                "debug_panels": debug_panels,
                "burst_api_map_report": api_map_report if args.consecutive_send else {},
                "burst_network_events": network_events if args.consecutive_send else [],
            },
        }

        report_path = out_dir / "prompt_media_map_live_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\nKẾT QUẢ LIVE TEST")
        print("-" * 72)
        for row in all_batches:
            print(
                f"prompt {int(row.get('prompt_index', 0)):02d}: "
                f"ok={bool(row.get('ok'))} "
                f"saved={int((all_capture_results[int(row.get('prompt_index', 1)) - 1] or {}).get('saved', 0) or 0)} "
                f"new_srcs={len(row.get('new_srcs', []) or [])} "
                f"files={len(row.get('generated_files', []) or [])}"
            )
        print(f"overlap src tổng : {int(overlap_report.get('total_overlap_count', 0) or 0)}")
        print(f"report json      : {report_path}")
        print("-" * 72)

        # Trả code 0 khi tất cả prompt đều map được ít nhất 1 ảnh và không có overlap URL giữa các prompt.
        all_ok = True
        for row in all_batches:
            ok = bool(row.get("ok")) and bool((row.get("generated_files") or []) or (row.get("new_srcs") or []))
            if not ok:
                all_ok = False
                break
        no_overlap = int(overlap_report.get("total_overlap_count", 0) or 0) == 0
        if all_ok and no_overlap:
            return 0
        return 2

    finally:
        # Đóng page (nếu còn) và dừng pool browser gọn gàng.
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass
        await pool.stop_all()


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Live integration test cho prompt_media_map_service bằng worker thật"
    )
    parser.add_argument("--worker", default="video_1", help="worker_id trong config/video_workers.json")
    parser.add_argument("--scenario", default="A", help="A hoặc kich_ban_A (chỉ dùng để gắn nhãn prompt test)")
    parser.add_argument("--prompt", default="", help="Prompt test thật. Nếu bỏ trống sẽ dùng prompt mặc định.")
    parser.add_argument(
        "--prompts",
        nargs="+",
        default=None,
        help="Danh sách nhiều prompt chạy liên tiếp trong cùng 1 project. Ví dụ: --prompts \"con mèo\" \"con chó\" \"con ngựa\"",
    )
    parser.add_argument("--expected", type=int, default=1, help="Số ảnh kỳ vọng cho prompt test.")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout chờ render (giây).")
    parser.add_argument("--no-proxy", action="store_true", help="Không dùng proxy dù config worker có proxy.")
    parser.add_argument(
        "--consecutive-send",
        action="store_true",
        help="Gửi prompt liên tiếp cách nhau --send-gap-sec giây, rồi mới capture tổng.",
    )
    parser.add_argument(
        "--send-gap-sec",
        type=float,
        default=2.0,
        help="Khoảng cách giây giữa các prompt khi bật --consecutive-send.",
    )
    parser.add_argument(
        "--debug-panel",
        dest="debug_panel",
        action="store_true",
        help="In DEBUG PANEL dạng JSON dễ đọc ngay trên terminal.",
    )
    parser.add_argument(
        "--no-debug-panel",
        dest="debug_panel",
        action="store_false",
        help="Tắt in DEBUG PANEL trên terminal (vẫn lưu report JSON).",
    )
    parser.set_defaults(debug_panel=True)
    parser.add_argument(
        "--debug-max-rows",
        type=int,
        default=12,
        help="Số dòng tối đa cho mỗi preview trong DEBUG PANEL.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    rc = asyncio.run(run_live_test(args))
    raise SystemExit(int(rc))
