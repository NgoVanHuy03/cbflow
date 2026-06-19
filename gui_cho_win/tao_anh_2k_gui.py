#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Giao diện tạo ảnh 2K bằng ChichBong Imagen4 (Tkinter — không cần cài thêm gì).

- Tự đọc license + machine identity từ Registry (Windows) hoặc file client.
- Nhập mô tả ảnh → bấm "TẠO ẢNH 2K" → ảnh lưu vào thư mục chọn.
- Mỗi dòng trong ô mô tả = 1 ảnh (bỏ qua dòng trống / bắt đầu bằng #).

Chạy:  python tao_anh_2k_gui.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

RATIO_MAP = {"Vuông (1:1)": "square", "Ngang (16:9)": "landscape", "Dọc (9:16)": "portrait"}
QUALITY_MAP = {"2K (nét hơn)": "2k", "1K (nhanh hơn)": "1k"}


# ── Đọc thông tin tài khoản (quota) từ Registry Windows ────────────────────────
def _read_account_info() -> dict:
    info: dict = {}
    if not sys.platform.startswith("win"):
        return info
    try:
        import winreg  # type: ignore
        path = r"Software\ElevenLabs\ElevenLabs TTS Client\account_imagen4"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            for name in ("email", "ngay_het_han", "max_images", "so_lan_tao_anh"):
                try:
                    v, _ = winreg.QueryValueEx(k, name)
                    info[name] = v
                except Exception:
                    pass
    except Exception:
        pass
    return info


def _mask(key: str) -> str:
    key = str(key or "")
    if len(key) <= 10:
        return key or "(không có)"
    return key[:8] + "…" + key[-4:]


# ── Forward log của engine vào hàng đợi để hiển thị trên GUI ───────────────────
class _QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", record.getMessage()))
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        self.running = False
        self.last_saved: list[Path] = []

        root.title("Tạo ảnh 2K — ChichBong Imagen4")
        root.geometry("640x620")
        root.minsize(560, 560)

        pad = {"padx": 12, "pady": 6}

        # Tiêu đề
        ttk.Label(root, text="Tạo ảnh 2K", font=("Segoe UI", 16, "bold")).pack(anchor="w", **pad)

        # Trạng thái license
        self.lbl_license = ttk.Label(root, text="Đang kiểm tra license…", foreground="#555")
        self.lbl_license.pack(anchor="w", padx=12)

        # Ô mô tả
        ttk.Label(root, text="Mô tả ảnh (mỗi dòng = 1 ảnh):").pack(anchor="w", **pad)
        self.txt = tk.Text(root, height=7, wrap="word", font=("Segoe UI", 10))
        self.txt.pack(fill="x", padx=12)
        self.txt.insert("1.0", "a golden retriever puppy sitting in a sunny garden, photorealistic")

        # Hàng tuỳ chọn: tỉ lệ + thư mục
        opt = ttk.Frame(root)
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="Tỉ lệ:").pack(side="left")
        self.cmb_ratio = ttk.Combobox(opt, values=list(RATIO_MAP.keys()), state="readonly", width=12)
        self.cmb_ratio.current(0)
        self.cmb_ratio.pack(side="left", padx=(4, 12))

        ttk.Label(opt, text="Chất lượng:").pack(side="left")
        self.cmb_quality = ttk.Combobox(opt, values=list(QUALITY_MAP.keys()), state="readonly", width=12)
        self.cmb_quality.current(0)  # mặc định 2K
        self.cmb_quality.pack(side="left", padx=(4, 16))

        ttk.Label(opt, text="Lưu vào:").pack(side="left")
        self.var_out = tk.StringVar(value=str(Path.cwd() / "anh_2k"))
        ttk.Entry(opt, textvariable=self.var_out).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(opt, text="Chọn…", command=self._choose_dir).pack(side="left")

        # Nút tạo
        self.btn = ttk.Button(root, text="TẠO ẢNH 2K", command=self._on_generate)
        self.btn.pack(fill="x", padx=12, pady=(8, 4))

        # Thanh tiến độ + trạng thái
        self.pbar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=12)
        self.lbl_status = ttk.Label(root, text="Sẵn sàng.", foreground="#1a7f37")
        self.lbl_status.pack(anchor="w", padx=12, pady=(2, 4))

        # Log
        ttk.Label(root, text="Nhật ký:").pack(anchor="w", padx=12)
        logf = ttk.Frame(root)
        logf.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.log = tk.Text(logf, height=8, state="disabled", wrap="word",
                           bg="#111", fg="#ddd", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Hàng nút cuối
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(bottom, text="Mở thư mục ảnh", command=self._open_folder).pack(side="left")
        self.btn_open_img = ttk.Button(bottom, text="Xem ảnh vừa tạo", command=self._open_last, state="disabled")
        self.btn_open_img.pack(side="left", padx=8)

        # Khởi tạo license + bắt đầu poll hàng đợi
        self._init_license()
        self.root.after(120, self._poll)

    # ── License ────────────────────────────────────────────────────────────────
    def _init_license(self):
        try:
            from services.chichbong_imagen_service import resolve_license_from_client
            self.license_key = resolve_license_from_client()
        except Exception as e:
            self.license_key = ""
            self._log(f"Lỗi nạp engine: {e}")

        acc = _read_account_info()
        if self.license_key:
            extra = ""
            if acc.get("ngay_het_han"):
                extra += f" | Hết hạn: {acc['ngay_het_han']}"
            if acc.get("max_images") not in (None, ""):
                extra += f" | Gói: {acc.get('max_images')} ảnh"
            self.lbl_license.config(
                text=f"License: {_mask(self.license_key)}{extra}", foreground="#1a7f37")
        else:
            self.lbl_license.config(
                text="⚠ Không tìm thấy license. Hãy cài & kích hoạt app ChichBong trên máy này.",
                foreground="#b00020")
            self.btn.config(state="disabled")

    # ── Hành động ───────────────────────────────────────────────────────────────
    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_out.get() or os.getcwd())
        if d:
            self.var_out.set(d)

    def _open_folder(self):
        out = Path(self.var_out.get())
        out.mkdir(parents=True, exist_ok=True)
        self._open_path(out)

    def _open_last(self):
        if self.last_saved:
            self._open_path(self.last_saved[-1])

    def _open_path(self, p: Path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(p)])
            else:
                subprocess.run(["xdg-open", str(p)])
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không mở được: {e}")

    def _on_generate(self):
        if self.running:
            return
        prompts = [ln.strip() for ln in self.txt.get("1.0", "end").splitlines()
                   if ln.strip() and not ln.strip().startswith("#")]
        if not prompts:
            messagebox.showwarning("Thiếu mô tả", "Hãy nhập ít nhất 1 dòng mô tả ảnh.")
            return
        if not self.license_key:
            messagebox.showerror("Thiếu license", "Không có license. Cài & kích hoạt app ChichBong.")
            return

        out_dir = Path(self.var_out.get())
        ratio = RATIO_MAP.get(self.cmb_ratio.get(), "square")
        quality = QUALITY_MAP.get(self.cmb_quality.get(), "2k")

        self.running = True
        self.btn.config(state="disabled", text="Đang tạo…")
        self.pbar.config(value=0)
        self.lbl_status.config(text=f"Đang tạo {len(prompts)} ảnh {quality.upper()}…", foreground="#0b66c3")
        self._log(f"--- Bắt đầu: {len(prompts)} ảnh, tỉ lệ={ratio}, {quality.upper()} → {out_dir} ---")

        t = threading.Thread(target=self._worker, args=(prompts, out_dir, ratio, quality), daemon=True)
        t.start()

    # ── Worker (thread riêng, chạy asyncio) ─────────────────────────────────────
    def _worker(self, prompts, out_dir: Path, ratio: str, quality: str):
        handler = _QueueLogHandler(self.q)
        eng_logger = logging.getLogger("services.chichbong_imagen_service")
        eng_logger.addHandler(handler)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            from services.chichbong_imagen_service import generate_images_with_chichbong
            saved = asyncio.run(generate_images_with_chichbong(
                prompts=prompts,
                output_dir=out_dir,
                license_key=self.license_key,
                aspect_ratio=ratio,
                upscale_mode=quality,
                max_in_flight=min(6, len(prompts)),
            ))
            self.q.put(("done", saved))
        except Exception as e:
            self.q.put(("error", str(e)))
        finally:
            eng_logger.removeHandler(handler)

    # ── Poll hàng đợi → cập nhật GUI (thread chính) ─────────────────────────────
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                    m = re.search(r"Tiến độ:\s*(\d+)%", str(data))
                    if m:
                        self.pbar.config(value=max(self.pbar["value"], int(m.group(1))))
                    if "Lưu ảnh" in str(data):
                        self.pbar.config(value=min(100, self.pbar["value"] + 10))
                elif kind == "done":
                    self._finish_done(data)
                elif kind == "error":
                    self._finish_error(data)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _finish_done(self, saved):
        self.running = False
        self.btn.config(state="normal", text="TẠO ẢNH 2K")
        self.pbar.config(value=100)
        self.last_saved = list(saved or [])
        n = len(self.last_saved)
        if n:
            self.lbl_status.config(text=f"✓ Xong: {n} ảnh đã lưu.", foreground="#1a7f37")
            self.btn_open_img.config(state="normal")
            self._log(f"--- Hoàn thành: {n} ảnh ---")
        else:
            self.lbl_status.config(text="✗ Không tạo được ảnh nào.", foreground="#b00020")
            self._log("--- Không có ảnh nào được tạo ---")

    def _finish_error(self, msg):
        self.running = False
        self.btn.config(state="normal", text="TẠO ẢNH 2K")
        self.lbl_status.config(text="✗ Lỗi — xem nhật ký.", foreground="#b00020")
        self._log(f"LỖI: {msg}")
        messagebox.showerror("Lỗi tạo ảnh", str(msg))

    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
