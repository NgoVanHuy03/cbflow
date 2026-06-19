#!/usr/bin/env python3
"""
Chạy Imagen4 độc lập — không cần flow, không cần sheet.

Cách dùng:
  # Từ file prompt (mỗi dòng 1 prompt):
  python run_imagen4.py --prompts-file my_prompts.txt

  # Hoặc truyền thẳng trên CLI:
  python run_imagen4.py -p "a cat on the moon" -p "a dog on Mars"

  # Với tuỳ chọn:
  python run_imagen4.py --prompts-file prompts.txt --ratio landscape --quality 2k --output ./output_imgs

License key ưu tiên theo thứ tự:
  1. --license-key trên CLI
  2. ENV: IMAGEN4_LICENSE_KEY hoặc CHICHBONG_LICENSE_KEY
  3. Tự dò từ file chichbong_api_client.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path


def _resolve_license_key(explicit: str) -> str:
    key = str(explicit or "").strip()
    if key:
        return key
    for env_name in ("IMAGEN4_LICENSE_KEY", "CHICHBONG_LICENSE_KEY"):
        v = str(os.environ.get(env_name, "") or "").strip()
        if v:
            print(f"[run_imagen4] Dùng license từ ENV {env_name}")
            return v
    candidates = [
        Path("/Users/may6/Downloads/chichbong/chichbongtaoanh/chichbong_api_client.py"),
    ]
    for p in candidates:
        try:
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r'^\s*LICENSE_KEY\s*=\s*["\']([^"\']+)["\']', text, flags=re.MULTILINE)
            if m:
                print(f"[run_imagen4] Auto nạp license từ: {p}")
                return m.group(1).strip()
        except Exception:
            continue
    return ""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tạo ảnh Imagen4 độc lập (không cần flow)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "-p", "--prompt",
        dest="prompts",
        action="append",
        default=[],
        metavar="TEXT",
        help="Prompt (có thể lặp nhiều lần: -p '...' -p '...')",
    )
    parser.add_argument(
        "--prompts-file", "-f",
        default="",
        help="File chứa danh sách prompts, mỗi dòng 1 prompt",
    )
    parser.add_argument(
        "--output", "-o",
        default="./imagen4_output",
        help="Thư mục lưu ảnh (mặc định: ./imagen4_output)",
    )
    parser.add_argument(
        "--ratio", "-r",
        default="square",
        choices=["square", "landscape", "portrait"],
        help="Tỷ lệ ảnh (mặc định: square)",
    )
    parser.add_argument(
        "--quality", "-q",
        default="2k",
        choices=["1k", "2k", "4k"],
        help="Chất lượng upscale (mặc định: 2k)",
    )
    parser.add_argument(
        "--in-flight",
        type=int,
        default=6,
        help="Số prompt chạy song song mỗi wave (1-6, mặc định: 6)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Seed ngẫu nhiên (0 = tự chọn)",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Dùng model cũ IMAGEN_3_5 thay vì Imagen4",
    )
    parser.add_argument(
        "--model-name",
        default="",
        help="Tên model tuỳ chọn (để trống = dùng mặc định)",
    )
    parser.add_argument(
        "--license-key", "-k",
        default="",
        help="License key ChichBong (để trống = tự dò từ ENV hoặc file client)",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    # Thu thập prompts
    prompts: list[str] = list(args.prompts)

    if args.prompts_file:
        pf = Path(args.prompts_file)
        if not pf.exists():
            print(f"[run_imagen4] Lỗi: không tìm thấy file '{pf}'", file=sys.stderr)
            sys.exit(1)
        lines = [ln.strip() for ln in pf.read_text(encoding="utf-8").splitlines()]
        prompts += [ln for ln in lines if ln and not ln.startswith("#")]

    if not prompts:
        print(
            "[run_imagen4] Lỗi: chưa có prompt nào.\n"
            "  Dùng -p 'prompt text' hoặc --prompts-file <file>",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve license key
    license_key = _resolve_license_key(args.license_key)
    if not license_key:
        print(
            "[run_imagen4] Lỗi: không tìm thấy license key.\n"
            "  Truyền qua --license-key hoặc set ENV IMAGEN4_LICENSE_KEY=...",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[run_imagen4] Bắt đầu tạo {len(prompts)} ảnh → {output_dir}")
    print(f"[run_imagen4] Ratio={args.ratio} | Quality={args.quality} | InFlight={args.in_flight} | Seed={args.seed or 'auto'}")

    # Import service (cùng thư mục)
    sys.path.insert(0, str(Path(__file__).parent))
    from services.chichbong_imagen_service import generate_images_with_chichbong

    saved = await generate_images_with_chichbong(
        prompts=prompts,
        output_dir=output_dir,
        license_key=license_key,
        aspect_ratio=args.ratio,
        use_legacy_model=args.legacy,
        seed=args.seed if args.seed > 0 else None,
        upscale_mode=args.quality,
        image_model_name=args.model_name or None,
        max_in_flight=args.in_flight,
    )

    print(f"\n[run_imagen4] Hoàn thành: {len(saved)}/{len(prompts)} ảnh đã lưu vào {output_dir}")
    for p in saved:
        print(f"  {p}")


if __name__ == "__main__":
    asyncio.run(main())
