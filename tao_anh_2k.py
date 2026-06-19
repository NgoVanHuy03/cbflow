#!/usr/bin/env python3
"""
Tạo ảnh chất lượng 2K bằng ChichBong Imagen4 — script riêng, đơn giản.

Chất lượng LUÔN cố định 2K (2048px). License + machine identity tự dò từ
file client ChichBong trên máy (hoặc set qua ENV IMAGEN4_LICENSE_KEY).

Cách dùng:
  # 1 prompt
  python3 tao_anh_2k.py -p "a golden retriever puppy in a sunny garden"

  # nhiều prompt
  python3 tao_anh_2k.py -p "prompt 1" -p "prompt 2"

  # đọc prompt từ file (mỗi dòng 1 prompt, bỏ qua dòng trống / bắt đầu bằng #)
  python3 tao_anh_2k.py -f prompts.txt

  # đổi tỉ lệ + thư mục lưu
  python3 tao_anh_2k.py -p "..." -r portrait -o ./anh_2k

Tỉ lệ (-r): square | landscape | portrait   (mặc định: square)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Cho phép import services khi chạy từ bất kỳ thư mục nào.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.chichbong_imagen_service import (
    generate_images_with_chichbong,
    resolve_license_from_client,
)

# Chất lượng khóa cứng 2K — đây là mục đích của script này.
QUALITY_2K = "2k"


def _resolve_license_key(explicit: str) -> str:
    """Dò license: tham số > ENV > file client ChichBong trên máy (cross-platform)."""
    key = str(explicit or "").strip()
    if key:
        return key
    for env_name in ("IMAGEN4_LICENSE_KEY", "CHICHBONG_LICENSE_KEY"):
        v = str(os.environ.get(env_name, "") or "").strip()
        if v:
            print(f"[2k] Dùng license từ ENV {env_name}")
            return v
    # Tự dò từ file client ChichBong (macOS / Windows / Linux).
    return resolve_license_from_client()


def _collect_prompts(args: argparse.Namespace) -> list[str]:
    prompts: list[str] = list(args.prompts or [])
    prompts += list(args.extra or [])  # prompt truyền dạng positional
    if args.prompts_file:
        pf = Path(args.prompts_file)
        if not pf.exists():
            print(f"[2k] Lỗi: không tìm thấy file '{pf}'", file=sys.stderr)
            sys.exit(1)
        for ln in pf.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                prompts.append(ln)
    # Bỏ trùng, giữ thứ tự.
    seen: set[str] = set()
    uniq: list[str] = []
    for p in prompts:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Tạo ảnh 2K bằng ChichBong Imagen4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-p", "--prompt", dest="prompts", action="append", default=[],
                   metavar="TEXT", help="Prompt (lặp nhiều lần được)")
    p.add_argument("extra", nargs="*", help="Prompt truyền trực tiếp (không cần -p)")
    p.add_argument("-f", "--prompts-file", default="",
                   help="File prompts, mỗi dòng 1 prompt")
    p.add_argument("-o", "--output", default="./anh_2k",
                   help="Thư mục lưu ảnh (mặc định: ./anh_2k)")
    p.add_argument("-r", "--ratio", default="square",
                   choices=["square", "landscape", "portrait"],
                   help="Tỉ lệ ảnh (mặc định: square)")
    p.add_argument("--seed", type=int, default=0, help="Seed (0 = ngẫu nhiên)")
    p.add_argument("--in-flight", type=int, default=6,
                   help="Số ảnh chạy song song mỗi wave (1-15, mặc định: 6)")
    p.add_argument("-k", "--license-key", default="",
                   help="License key ChichBong (để trống = tự dò)")
    return p.parse_args()


async def _run() -> int:
    args = _parse_args()

    prompts = _collect_prompts(args)
    if not prompts:
        print("[2k] Lỗi: chưa có prompt nào.\n"
              "  Dùng: tao_anh_2k.py -p 'mô tả ảnh'  hoặc  -f prompts.txt",
              file=sys.stderr)
        return 1

    license_key = _resolve_license_key(args.license_key)
    if not license_key:
        print("[2k] Lỗi: không tìm thấy license key.\n"
              "  Set ENV IMAGEN4_LICENSE_KEY=... hoặc truyền --license-key",
              file=sys.stderr)
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[2k] Tạo {len(prompts)} ảnh 2K → {output_dir}")
    print(f"[2k] Tỉ lệ={args.ratio} | Quality={QUALITY_2K} | "
          f"InFlight={args.in_flight} | Seed={args.seed or 'auto'}")

    saved = await generate_images_with_chichbong(
        prompts=prompts,
        output_dir=output_dir,
        license_key=license_key,
        aspect_ratio=args.ratio,
        seed=args.seed if args.seed > 0 else None,
        upscale_mode=QUALITY_2K,
        max_in_flight=max(1, min(15, int(args.in_flight))),
    )

    print(f"\n[2k] Hoàn thành: {len(saved)}/{len(prompts)} ảnh đã lưu vào {output_dir}")
    for path in saved:
        print(f"  {path}")
    return 0 if saved else 2


def main() -> None:
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        print("\n[2k] Đã hủy.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
