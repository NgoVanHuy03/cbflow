from __future__ import annotations

"""
ChichBong Imagen4 - Image generation engine dùng WebSocket API.

Interface tương thích với flow engine: nhận prompts -> trả list[Path].
Module này đã được mở rộng theo bản client terminal ở:
  /Users/may6/Downloads/chichbong/chichbongtaoanh/chichbong_api_client.py
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)
logger.propagate = False

# ── Cấu hình API ─────────────────────────────────────────────────────────────
WEBSOCKET_URL = str(os.environ.get("CHICHBONG_WEBSOCKET_URL", "wss://api.chichbong.me/") or "").strip() or "wss://api.chichbong.me/"
API_BASE_URL = str(os.environ.get("CHICHBONG_API_BASE_URL", "https://11labs.net/api") or "").strip() or "https://11labs.net/api"
BRAND = "imagen4"
VERSION = "1.3.5"

# Machine identity — BẮT BUỘC set qua ENV (đã để trống khi public source).
#   CHICHBONG_HARDWARE_ID, CHICHBONG_CPU_ID, CHICHBONG_MAINBOARD_UUID
HARDWARE_ID = str(os.environ.get("CHICHBONG_HARDWARE_ID", "") or "").strip()
CPU_ID = str(os.environ.get("CHICHBONG_CPU_ID", "") or "").strip()
MAINBOARD_UUID = str(os.environ.get("CHICHBONG_MAINBOARD_UUID", "") or "").strip()

MAX_ROUNDS = 10
TIMEOUT_SMALL = 300   # <50 prompt → 300s
TIMEOUT_LARGE = 300   # >=50 prompt → 300s
PING_SMALL = 90           # ping giữ mạng <50 prompt
PING_LARGE = 120          # ping giữ mạng >=50 prompt
MAX_WS_PAYLOAD = 20 * 1024 * 1024  # 20 MB

ASPECT_RATIO_MAP = {
    "square": "IMAGE_ASPECT_RATIO_SQUARE",
    "landscape": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "portrait": "IMAGE_ASPECT_RATIO_PORTRAIT",
    "IMAGE_ASPECT_RATIO_SQUARE": "IMAGE_ASPECT_RATIO_SQUARE",
    "IMAGE_ASPECT_RATIO_LANDSCAPE": "IMAGE_ASPECT_RATIO_LANDSCAPE",
    "IMAGE_ASPECT_RATIO_PORTRAIT": "IMAGE_ASPECT_RATIO_PORTRAIT",
}

UPSCALE_MODE_MAP = {
    "1k": "1k",
    "2k": "2k",
    "4k": "4k",
}


def _extract_literal(text: str, name: str) -> str:
    m = re.search(rf'^\s*{re.escape(name)}\s*=\s*["\']([^"\']+)["\']', str(text or ""), flags=re.MULTILINE)
    return str(m.group(1) if m else "").strip()


def _candidate_external_client_files() -> list[Path]:
    """
    Danh sách vị trí có thể chứa file client ChichBong (chichbong_api_client.py)
    — file này giữ LICENSE_KEY + machine identity. Hoạt động cross-platform
    (macOS / Linux / Windows). Thứ tự ưu tiên:
      1) ENV CHICHBONG_CLIENT_FILE (trỏ thẳng file — ưu tiên cao nhất)
      2) Đường dẫn tương đối theo cấu trúc thư mục dự án
      3) Các thư mục người dùng phổ biến (Downloads/Desktop/Documents/home)
         + thư mục hệ thống Windows (USERPROFILE/APPDATA/PROGRAMFILES...)
    """
    out: list[Path] = []
    rel = ("chichbong", "chichbongtaoanh", "chichbong_api_client.py")

    # 1) ENV trỏ thẳng file
    env_path = str(os.environ.get("CHICHBONG_CLIENT_FILE", "") or "").strip()
    if env_path:
        out.append(Path(env_path))

    # 2) Tương đối theo dự án: <.../flow_video/short-image-flow> → lên 2 cấp
    project_root = Path(__file__).resolve().parent.parent
    out.append(project_root.parent.parent.joinpath(*rel))

    # 3) Các gốc thư mục phổ biến (cross-platform)
    roots: list[Path] = []
    try:
        home = Path.home()
        roots += [home / "Downloads", home / "Desktop", home / "Documents", home]
    except Exception:
        pass
    for env_name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        v = str(os.environ.get(env_name, "") or "").strip()
        if v:
            roots.append(Path(v))
    for root in roots:
        out.append(root.joinpath(*rel))

    # 4) Đường dẫn macOS cũ (tương thích ngược)
    out.append(Path("/Users/may6/Downloads").joinpath(*rel))

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        k = str(p)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def resolve_license_from_client() -> str:
    """
    Tự dò LICENSE_KEY từ file client ChichBong trên máy (cross-platform).
    Trả về chuỗi rỗng nếu không tìm thấy.
    """
    for path in _candidate_external_client_files():
        try:
            if not path.exists() or not path.is_file():
                continue
            key = _extract_literal(path.read_text(encoding="utf-8", errors="ignore"), "LICENSE_KEY")
            if key:
                logger.info(f"[cb-imagen] Auto nạp license từ: {path}")
                return key
        except Exception:
            continue
    return ""


_EXT_CLIENT_CACHE: dict[str, str] | None = None


def _load_external_client_constants() -> dict[str, str]:
    global _EXT_CLIENT_CACHE
    if _EXT_CLIENT_CACHE is not None:
        return _EXT_CLIENT_CACHE

    for path in _candidate_external_client_files():
        try:
            if not path.exists() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            data = {
                "HARDWARE_ID": _extract_literal(text, "HARDWARE_ID"),
                "CPU_ID": _extract_literal(text, "CPU_ID"),
                "MAINBOARD_UUID": _extract_literal(text, "MAINBOARD_UUID"),
            }
            if data["HARDWARE_ID"] and data["CPU_ID"] and data["MAINBOARD_UUID"]:
                logger.info(f"[cb-imagen] Auto nạp machine identity từ: {path}")
                _EXT_CLIENT_CACHE = data
                return data
        except Exception:
            continue

    _EXT_CLIENT_CACHE = {}
    return _EXT_CLIENT_CACHE


def _resolve_machine_identity() -> tuple[str, str, str]:
    ext = _load_external_client_constants()
    hw = str(os.environ.get("CHICHBONG_HARDWARE_ID", "") or "").strip() or ext.get("HARDWARE_ID", "") or HARDWARE_ID
    cpu = str(os.environ.get("CHICHBONG_CPU_ID", "") or "").strip() or ext.get("CPU_ID", "") or CPU_ID
    mb = str(os.environ.get("CHICHBONG_MAINBOARD_UUID", "") or "").strip() or ext.get("MAINBOARD_UUID", "") or MAINBOARD_UUID
    return str(hw), str(cpu), str(mb)


def _check_deps() -> None:
    if requests is None:
        raise RuntimeError("Thiếu thư viện 'requests'. Chạy: pip install requests")
    if websockets is None:
        raise RuntimeError("Thiếu thư viện 'websockets'. Chạy: pip install websockets")


# ── REST API client ───────────────────────────────────────────────────────────

class _ChichBongAPIClient:
    def __init__(self, license_key: str) -> None:
        self.license_key = license_key
        self.base_url = API_BASE_URL
        self.timeout = 15
        self.headers = {
            "User-Agent": "Imagen4-Client/1.0",
            "Content-Type": "application/json",
        }

    def verify_license(self) -> bool:
        """POST /license/verify_imagen4.php → kiểm tra license + mã máy."""
        url = f"{self.base_url}/license/verify_imagen4.php"
        hw, cpu, mb = _resolve_machine_identity()
        payload = {
            "license_key": self.license_key,
            "hardware_id": hw,
            "cpu_id": cpu,
            "mainboard_uuid": mb,
            "brand": BRAND,
            "current_version": VERSION,
        }
        try:
            r = requests.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            result = r.json()
            ok = bool(result.get("success"))
            if ok:
                logger.info("[cb-imagen] License hợp lệ.")
            else:
                logger.error(f"[cb-imagen] License không hợp lệ: {result.get('message')}")
            return ok
        except Exception as exc:
            logger.error(f"[cb-imagen] Lỗi verify license: {exc}")
            return False

    def account_info(self) -> dict:
        """GET /account/info → thông tin tài khoản (email, imagen_count, imagen_per_day…)."""
        url = f"{self.base_url}/account/info"
        try:
            r = requests.get(
                url,
                params={"license_key": self.license_key, "app": BRAND},
                headers=self.headers,
                timeout=self.timeout,
            )
            return r.json()
        except Exception as exc:
            logger.warning(f"[cb-imagen] account_info thất bại: {exc}")
            return {}

    def get_tokens(self, limit: int = 5) -> list[dict]:
        """
        GET /checker/get-imagen4-token.php
        Lấy token theo luồng client gốc (dự phòng/diagnostic).
        """
        _ = limit  # API hiện trả theo server-side policy; giữ tham số để tương thích.
        url = f"{self.base_url}/checker/get-imagen4-token.php"
        try:
            r = requests.get(
                url,
                params={"license_key": self.license_key},
                headers=self.headers,
                timeout=30,
            )
            if r.status_code != 200:
                logger.warning(f"[cb-imagen] get_tokens HTTP {r.status_code}: {r.text[:240]}")
                return []
            data = r.json()
            if not data.get("success"):
                logger.warning(f"[cb-imagen] get_tokens fail: {data.get('message')}")
                return []
            tokens = data.get("tokens", [])
            return tokens if isinstance(tokens, list) else []
        except Exception as exc:
            logger.warning(f"[cb-imagen] get_tokens exception: {exc}")
            return []

    def report_usage(self, count: int) -> None:
        """POST /resource/report-imagen-counter.php → báo số ảnh tạo thành công."""
        if count <= 0:
            return
        url = f"{self.base_url}/resource/report-imagen-counter.php"
        try:
            requests.post(
                url,
                json={"license_key": self.license_key, "successful_count": count},
                headers=self.headers,
                timeout=self.timeout,
            )
            logger.info(f"[cb-imagen] Đã báo cáo {count} ảnh thành công.")
        except Exception as exc:
            logger.warning(f"[cb-imagen] report_usage thất bại (không quan trọng): {exc}")


# ── WebSocket client ──────────────────────────────────────────────────────────

class _ChichBongWSClient:
    def __init__(
        self,
        license_key: str,
        output_dir: Path,
        scene_names: dict[int, str] | None = None,
    ) -> None:
        self.license_key = license_key
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Map scene_no → tên file tuỳ chỉnh (không đuôi). Dùng cho ảnh nhân vật
        # để lưu theo label (vd CHAR01_NAOMI_HAYES_LOOK1) thay vì canh_001.
        self.scene_names: dict[int, str] = dict(scene_names or {})

    async def generate(
        self,
        prompts: list[str],
        prompt_indices: list[int] | None = None,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_SQUARE",
        use_legacy_model: bool = False,
        seed: int | None = None,
        upscale_mode: str = "1k",
        image_model_name: str | None = None,
    ) -> list[Path]:
        """
        Gửi prompts, nhận ảnh qua WebSocket.
        Tự động retry qua kết nối mới nếu mất mạng giữa chừng (max_rounds=8).
        """
        _check_deps()

        import ssl
        try:
            import certifi

            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ssl_ctx = ssl.create_default_context()

        total = len(prompts)
        if total <= 0:
            return []
        if prompt_indices is None:
            prompt_indices = list(range(1, total + 1))
        if len(prompt_indices) != total:
            raise ValueError("prompt_indices phải cùng độ dài với prompts")

        base_seed = seed or (int(time.time()) % 100000)
        normalized_upscale = UPSCALE_MODE_MAP.get(str(upscale_mode or "").lower(), "1k")
        model_name = str(image_model_name or "").strip()

        # Gán stable prompt_id cho mỗi prompt; key = prompt_id, value = scene no tuyệt đối
        pid_to_scene: dict[str, int] = {}
        all_items: list[dict] = []
        for i, text in enumerate(prompts):
            scene_no = int(prompt_indices[i])
            pid = f"pc_{uuid.uuid4().hex}_{i}"
            pid_to_scene[pid] = scene_no

            item: dict = {
                "prompt_id": pid,
                "prompt": text,
                "aspect_ratio": aspect_ratio,
                "seed": base_seed,
                "index": max(0, scene_no - 1),
                "original_prompt": text,
                "attachment": None,
            }

            # Đồng bộ hành vi từ client chichbong_api_client.py
            if use_legacy_model:
                item["use_legacy_model"] = True
                item["image_model_name"] = "IMAGEN_3_5"
            elif model_name:
                item["image_model_name"] = model_name

            if normalized_upscale in {"2k", "4k"}:
                item["upscale_mode"] = normalized_upscale

            all_items.append(item)

        saved_by_scene: dict[int, Path] = {}  # scene_no → file path
        pending_pids: set[str] = set(pid_to_scene.keys())

        timeout_sec = TIMEOUT_LARGE if total >= 50 else TIMEOUT_SMALL
        ping_interval = PING_LARGE if total >= 50 else PING_SMALL
        ping_timeout = 90 if total >= 50 else 60
        rounds_used = 0

        for rnd in range(1, MAX_ROUNDS + 1):
            rounds_used = rnd
            if not pending_pids:
                break

            pending_items = [it for it in all_items if it["prompt_id"] in pending_pids]
            n_pending = len(pending_items)
            logger.info(
                f"[cb-imagen] Vòng {rnd}/{MAX_ROUNDS}: kết nối WS | "
                f"còn {n_pending}/{total} prompt chưa có ảnh"
            )

            try:
                async with websockets.connect(
                    WEBSOCKET_URL,
                    ssl=ssl_ctx,
                    ping_interval=ping_interval,
                    ping_timeout=ping_timeout,
                    open_timeout=30,
                    max_size=MAX_WS_PAYLOAD,
                    compression=None,
                ) as ws:
                    # 1) Đăng ký
                    await ws.send(
                        json.dumps(
                            {"event": "register", "data": {"license_key": self.license_key}}
                        )
                    )
                    reg_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if reg_resp.get("event") != "registered":
                        logger.warning(f"[cb-imagen] Register thất bại vòng {rnd}: {reg_resp}")
                        await asyncio.sleep(10)
                        continue

                    client_id = reg_resp.get("data", {}).get("client_id", "?")
                    logger.info(f"[cb-imagen] Đăng ký OK | client_id={client_id}")

                    # 2) Gửi batch pending
                    await ws.send(
                        json.dumps(
                            {"event": "submit_prompt_batch", "data": pending_items}
                        )
                    )
                    logger.info(f"[cb-imagen] Gửi {n_pending} prompts.")

                    # 3) Nhận kết quả
                    round_received = 0
                    round_failed = 0
                    # Giới hạn cứng tổng thời gian chờ mỗi vòng. Gia hạn khi nhận
                    # ảnh KHÔNG được vượt quá mốc này → tránh kẹt chờ vài ảnh cuối.
                    hard_deadline = time.time() + timeout_sec
                    deadline = hard_deadline

                    while time.time() < deadline:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 60))
                            msg = json.loads(raw)
                            event = msg.get("event", "")

                            if event == "prompt_result":
                                d = msg.get("data", {})
                                pid = str(d.get("prompt_id", "") or "")
                                status = str(d.get("status", "") or "")
                                img_b64 = str(d.get("image_base64", "") or "")

                                if status == "success" and img_b64:
                                    scene_no = int(pid_to_scene.get(pid, 0) or 0)
                                    fpath = self._save(img_b64, scene_no)
                                    if fpath and scene_no > 0:
                                        saved_by_scene[scene_no] = fpath
                                        pending_pids.discard(pid)
                                        round_received += 1
                                        # Gia hạn deadline mỗi khi nhận ảnh thành công,
                                        # nhưng không vượt quá giới hạn cứng tổng (timeout_sec).
                                        deadline = min(hard_deadline, max(deadline, time.time() + 300))
                                        logger.info(
                                            f"[cb-imagen] Lưu ảnh [{len(saved_by_scene)}/{total}]: {fpath.name}"
                                        )
                                else:
                                    err = d.get("error", status)
                                    logger.warning(f"[cb-imagen] Prompt failed: {err}")
                                    if pid:
                                        pending_pids.discard(pid)
                                    round_failed += 1

                                if not pending_pids:
                                    break

                            elif event == "prompt_queued":
                                pos = msg.get("data", {}).get("queue_position", "?")
                                logger.info(f"[cb-imagen] Đang xếp hàng, vị trí: {pos}")

                            elif event == "task_status":
                                pct = msg.get("data", {}).get("progress", "?")
                                logger.info(f"[cb-imagen] Tiến độ: {pct}%")

                            elif event == "stats":
                                qs = msg.get("data", {}).get("queue_size", "?")
                                logger.info(f"[cb-imagen] Queue server: {qs} người")

                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[cb-imagen] Chờ server vòng {rnd} "
                                f"({round_received} xong, {len(pending_pids)} còn lại, "
                                f"còn {max(0, deadline - time.time()):.0f}s)"
                            )

                    logger.info(
                        f"[cb-imagen] Vòng {rnd}: +{round_received} thành công, "
                        f"+{round_failed} thất bại, còn {len(pending_pids)} chờ retry"
                    )

            except Exception as exc:
                logger.warning(f"[cb-imagen] Lỗi kết nối WS vòng {rnd}: {exc}")
                await asyncio.sleep(5)

        # Trả về theo đúng thứ tự scene tuyệt đối.
        saved = [saved_by_scene[i] for i in sorted(saved_by_scene)]
        logger.info(
            f"[cb-imagen] Tổng kết: {len(saved)}/{total} ảnh thành công sau {rounds_used} vòng"
        )
        return saved

    def _save(self, b64_data: str, scene_no: int) -> Path | None:
        try:
            if "," in b64_data:
                b64_data = b64_data.split(",", 1)[1]
            img_bytes = base64.b64decode(b64_data)
            custom = self.scene_names.get(int(scene_no), "")
            if custom:
                fname = f"{custom}.jpg"
            elif int(scene_no) > 0:
                fname = f"canh_{int(scene_no):03d}.jpg"
            else:
                fname = f"cb_{uuid.uuid4().hex[:8]}.jpg"
            fpath = self.output_dir / fname
            fpath.write_bytes(img_bytes)
            return fpath
        except Exception as exc:
            logger.error(f"[cb-imagen] Lỗi lưu ảnh scene={scene_no}: {exc}")
            return None


# ── Public interface ──────────────────────────────────────────────────────────

async def generate_images_with_chichbong(
    prompts: list[str],
    output_dir: Path,
    license_key: str,
    aspect_ratio: str = "IMAGE_ASPECT_RATIO_SQUARE",
    use_legacy_model: bool = False,
    seed: int | None = None,
    upscale_mode: str = "1k",
    image_model_name: str | None = None,
    max_in_flight: int = 6,
    fetch_account_info: bool = False,
    fetch_tokens: bool = False,
) -> list[Path]:
    """
    Tạo ảnh qua ChichBong Imagen4 WebSocket API.

    Args:
        prompts: danh sách prompt text
        output_dir: thư mục lưu ảnh output
        license_key: license key ChichBong
        aspect_ratio: square | landscape | portrait hoặc giá trị API đầy đủ
        use_legacy_model: dùng model cũ IMAGEN_3_5
        seed: seed ngẫu nhiên (None = tự chọn)
        upscale_mode: 1k | 2k | 4k
        image_model_name: model name tuỳ chọn (khi không dùng legacy)
        max_in_flight: số prompt chạy đồng thời mỗi wave (khuyến nghị 1-15)
        fetch_account_info: gọi thêm endpoint account_info để log/chẩn đoán
        fetch_tokens: gọi thêm endpoint get_tokens để chẩn đoán

    Returns:
        list[Path] ảnh đã lưu theo thứ tự prompt, rỗng nếu hoàn toàn thất bại
    """
    _check_deps()
    ratio = ASPECT_RATIO_MAP.get(aspect_ratio, "IMAGE_ASPECT_RATIO_SQUARE")

    api = _ChichBongAPIClient(license_key)
    if not api.verify_license():
        raise RuntimeError("[cb-imagen] License không hợp lệ, dừng generate.")

    if fetch_account_info:
        _ = api.account_info()
    if fetch_tokens:
        _ = api.get_tokens()

    ws_client = _ChichBongWSClient(license_key=license_key, output_dir=output_dir)
    cap = max(1, min(15, int(max_in_flight or 1)))
    scene_indices = list(range(1, len(prompts) + 1))
    all_saved: dict[int, Path] = {}

    for start in range(0, len(prompts), cap):
        wave_prompts = prompts[start:start + cap]
        wave_indices = scene_indices[start:start + cap]
        wave_no = (start // cap) + 1
        wave_total = (len(prompts) + cap - 1) // cap
        logger.info(
            f"[cb-imagen] Wave {wave_no}/{wave_total} | in_flight={len(wave_prompts)} (cap={cap})"
        )

        saved_wave = await ws_client.generate(
            prompts=wave_prompts,
            prompt_indices=wave_indices,
            aspect_ratio=ratio,
            use_legacy_model=use_legacy_model,
            seed=seed,
            upscale_mode=upscale_mode,
            image_model_name=image_model_name,
        )
        for p in saved_wave:
            m = re.search(r"canh_(\d{1,4})\.(?:png|jpe?g|webp)$", p.name, flags=re.IGNORECASE)
            if not m:
                continue
            scene_no = int(m.group(1))
            all_saved[scene_no] = p

    saved = [all_saved[i] for i in sorted(all_saved)]

    if saved:
        api.report_usage(len(saved))

    return saved


def _safe_basename(label: str, fallback: str) -> str:
    """Chuẩn hóa label thành tên file an toàn (giữ chữ/số/_/-)."""
    name = re.sub(r"[^\w\-]", "_", str(label or "").strip())
    name = re.sub(r"_{2,}", "_", name).strip("_")
    return name or fallback


async def generate_character_images_with_chichbong(
    entries: list[tuple[str, str]],
    output_dir: Path,
    license_key: str,
    aspect_ratio: str = "IMAGE_ASPECT_RATIO_SQUARE",
    use_legacy_model: bool = False,
    seed: int | None = None,
    upscale_mode: str = "1k",
    image_model_name: str | None = None,
    fetch_account_info: bool = False,
) -> list[Path]:
    """
    Tạo ảnh NHÂN VẬT qua ChichBong Imagen4 WebSocket API.

    Khác với generate_images_with_chichbong (lưu canh_001.jpg theo thứ tự cảnh),
    hàm này lưu mỗi ảnh theo LABEL của nhân vật, ví dụ:
        CHAR01_NAOMI_HAYES_LOOK1.jpg

    Args:
        entries: danh sách (label, prompt) — mỗi cặp tạo 1 ảnh
        output_dir: thư mục lưu ảnh nhân vật (vd output/characters)
        license_key: license key ChichBong
        (các tham số còn lại giống generate_images_with_chichbong)

    Returns:
        list[Path] ảnh đã lưu theo thứ tự entries, rỗng nếu hoàn toàn thất bại.
    """
    _check_deps()
    ratio = ASPECT_RATIO_MAP.get(aspect_ratio, "IMAGE_ASPECT_RATIO_SQUARE")

    clean: list[tuple[str, str]] = []
    for i, (label, prompt) in enumerate(entries):
        text = str(prompt or "").strip()
        if not text:
            continue
        basename = _safe_basename(label, fallback=f"nhan_vat_{i + 1:03d}")
        clean.append((basename, text))
    if not clean:
        return []

    api = _ChichBongAPIClient(license_key)
    if not api.verify_license():
        raise RuntimeError("[cb-imagen] License không hợp lệ, dừng generate nhân vật.")
    if fetch_account_info:
        _ = api.account_info()

    prompts = [p for _, p in clean]
    indices = list(range(1, len(prompts) + 1))
    scene_names = {idx: clean[idx - 1][0] for idx in indices}

    ws_client = _ChichBongWSClient(
        license_key=license_key,
        output_dir=output_dir,
        scene_names=scene_names,
    )
    logger.info(f"[cb-imagen] Tạo {len(prompts)} ảnh nhân vật → {output_dir}")
    saved = await ws_client.generate(
        prompts=prompts,
        prompt_indices=indices,
        aspect_ratio=ratio,
        use_legacy_model=use_legacy_model,
        seed=seed,
        upscale_mode=upscale_mode,
        image_model_name=image_model_name,
    )

    if saved:
        api.report_usage(len(saved))

    return saved
