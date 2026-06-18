"""
Test cho prompt_media_map_service.

Mục tiêu test:
1) Xác nhận service map đúng 1 prompt ra nhiều ảnh (N file).
2) Xác nhận thứ tự chọn ảnh "primary" ổn định theo rule hiện tại.
3) Xác nhận metadata batch (prompt_index, expected_count, new_srcs...) đúng như request đầu vào.

Cách chạy:
    python3 -m unittest tests/test_prompt_media_map_service.py -v
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from services.prompt_media_map_service import (
    build_prompt_media_batch,
    pick_primary_generated_file,
)


class TestPromptMediaMapService(unittest.TestCase):
    """Nhóm test chính cho service map prompt -> media batch."""

    def _write_fake_png(self, path: str, size_bytes: int, mtime: float | None = None) -> None:
        """
        Tạo file giả lập ảnh PNG.

        Lưu ý:
        - Nội dung file không cần là PNG hợp lệ vì service chỉ đọc size + mtime để sort.
        - size_bytes giúp mô phỏng ảnh lớn/nhỏ khác nhau.
        """
        with open(path, "wb") as f:
            f.write(b"0" * max(1, int(size_bytes)))
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def test_map_one_prompt_to_multiple_images(self) -> None:
        """
        Case chính: 1 prompt sinh ra nhiều ảnh.

        Kỳ vọng:
        - batch.generated_files chứa đủ các file đúng prefix.
        - batch.new_srcs giữ nguyên danh sách URL mới từ capture_result.
        - batch.ok = True khi capture_result.ok=True và có dữ liệu.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prefix = "kich_ban_A_001"

            # Giả lập 3 ảnh được tải về cho cùng 1 prompt.
            self._write_fake_png(os.path.join(tmp, f"{prefix}_img1.png"), 100)
            self._write_fake_png(os.path.join(tmp, f"{prefix}_img2.png"), 200)
            self._write_fake_png(os.path.join(tmp, f"{prefix}_img3.png"), 150)

            # Giả lập response từ run_flow_image_capture.
            capture_result = {
                "ok": True,
                "saved": 3,
                "new_srcs": [
                    "https://img.example/1.png",
                    "https://img.example/2.png",
                    "https://img.example/3.png",
                ],
            }

            batch = build_prompt_media_batch(
                mode="scene",
                prompt_index=1,
                prompt_total=82,
                prompt_text="CẢNH 1: test prompt",
                output_dir=tmp,
                prefix=prefix,
                expected_count=1,
                capture_result=capture_result,
            )

            self.assertTrue(batch.ok)
            self.assertEqual(batch.prompt_index, 1)
            self.assertEqual(batch.prompt_total, 82)
            self.assertEqual(batch.expected_count, 1)
            self.assertEqual(batch.saved_count, 3)
            self.assertEqual(len(batch.new_srcs), 3)
            self.assertEqual(len(batch.generated_files), 3)

    def test_primary_file_pick_prefers_larger_file(self) -> None:
        """
        Rule chọn primary hiện tại:
        - Ưu tiên file size lớn hơn.
        - Nếu cùng size thì ưu tiên mtime mới hơn.

        Case này kiểm tra nhánh "ưu tiên size".
        """
        with tempfile.TemporaryDirectory() as tmp:
            prefix = "CHAR01_TEST"

            p1 = os.path.join(tmp, f"{prefix}_img1.png")
            p2 = os.path.join(tmp, f"{prefix}_img2.png")
            p3 = os.path.join(tmp, f"{prefix}_img3.png")

            # img2 lớn nhất -> phải thành primary.
            self._write_fake_png(p1, 111)
            self._write_fake_png(p2, 999)
            self._write_fake_png(p3, 555)

            batch = build_prompt_media_batch(
                mode="reference",
                prompt_index=1,
                prompt_total=1,
                prompt_text="CHAR01 prompt",
                output_dir=tmp,
                prefix=prefix,
                expected_count=1,
                capture_result={"ok": True, "saved": 1, "new_srcs": []},
            )

            primary = pick_primary_generated_file(batch)
            self.assertEqual(primary, p2)

    def test_primary_file_pick_tie_break_by_mtime(self) -> None:
        """
        Khi size bằng nhau, service phải chọn file mới hơn theo mtime.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prefix = "CHAR02_TEST"
            now = time.time()

            old_file = os.path.join(tmp, f"{prefix}_img1.png")
            new_file = os.path.join(tmp, f"{prefix}_img2.png")

            # Cùng size 300 byte, nhưng img2 mới hơn.
            self._write_fake_png(old_file, 300, mtime=now - 100)
            self._write_fake_png(new_file, 300, mtime=now)

            batch = build_prompt_media_batch(
                mode="reference",
                prompt_index=1,
                prompt_total=1,
                prompt_text="CHAR02 prompt",
                output_dir=tmp,
                prefix=prefix,
                expected_count=1,
                capture_result={"ok": True, "saved": 2, "new_srcs": []},
            )

            primary = pick_primary_generated_file(batch)
            self.assertEqual(primary, new_file)

    def test_batch_not_ok_when_capture_not_ok(self) -> None:
        """
        Khi capture_result.ok=False thì batch.ok phải False.
        Dùng để phát hiện prompt fail rõ ràng.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prefix = "kich_ban_A_999"
            self._write_fake_png(os.path.join(tmp, f"{prefix}_img1.png"), 123)

            batch = build_prompt_media_batch(
                mode="scene",
                prompt_index=99,
                prompt_total=99,
                prompt_text="prompt fail",
                output_dir=tmp,
                prefix=prefix,
                expected_count=1,
                capture_result={"ok": False, "saved": 0, "new_srcs": []},
            )

            self.assertFalse(batch.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
