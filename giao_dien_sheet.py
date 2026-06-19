#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Giao diện chạy pipeline: Google Sheet → tạo ảnh (imagen4) → upload Drive → ghi link về Sheet.

Trên giao diện:
  1. Chọn file credentials (.json) — chìa khóa Google API để đọc/ghi sheet.
  2. Đăng nhập Google (mở trình duyệt xác thực, tạo token.json).
  3. Chọn file danh sách sheet (.txt) — mỗi dòng 1 link sheet (có thể kèm |hoa).
  4. Chọn folder Drive lưu ảnh (mặc định: root = My Drive).
  5. Bấm BẮT ĐẦU CHẠY.

License imagen4 tự đọc từ Registry máy (cần app ChichBong đã kích hoạt).

Chạy:  python giao_dien_sheet.py
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_DIR = Path(__file__).resolve().parent


# ── Redirect print() của pipeline vào hàng đợi GUI ─────────────────────────────
class _QueueWriter:
    def __init__(self, q: "queue.Queue"):
        self.q = q

    def write(self, s):
        s = str(s)
        if s.strip():
            self.q.put(("log", s.rstrip("\n")))

    def flush(self):
        pass


def _parse_sources(path: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        sheet, mode = line, "black-auto"
        if "|" in line:
            left, right = line.split("|", 1)
            sheet = left.strip()
            mode = (right.strip() or "black-auto").lower()
        if sheet:
            items.append((sheet, mode))
    return items


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        self.running = False
        self.stop_flag = False
        self.logged_in = False
        self.token_path = str(APP_DIR / "token.json")

        self.cred_path = tk.StringVar()
        self.sources_path = tk.StringVar()
        self.drive_folder = tk.StringVar(value="root")

        root.title("Chạy Sheet → Ảnh → Drive (ChichBong Imagen4)")
        root.geometry("680x640")
        root.minsize(600, 580)
        pad = {"padx": 12, "pady": 4}

        ttk.Label(root, text="Chạy nhiều Sheet tự động",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w", **pad)

        # License imagen4
        self.lbl_license = ttk.Label(root, text="Đang kiểm tra license…", foreground="#555")
        self.lbl_license.pack(anchor="w", padx=12)

        # 1) Credentials
        f1 = ttk.LabelFrame(root, text="1) File credentials (.json)")
        f1.pack(fill="x", **pad)
        ttk.Entry(f1, textvariable=self.cred_path).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f1, text="Chọn file .json", command=self._pick_cred).pack(side="left", padx=6)

        # 2) Đăng nhập Google
        f2 = ttk.Frame(root)
        f2.pack(fill="x", **pad)
        self.btn_login = ttk.Button(f2, text="2) Đăng nhập Google", command=self._login)
        self.btn_login.pack(side="left")
        self.lbl_login = ttk.Label(f2, text="● Chưa đăng nhập", foreground="#b00020")
        self.lbl_login.pack(side="left", padx=10)

        # 3) Sources .txt
        f3 = ttk.LabelFrame(root, text="3) File danh sách sheet (.txt)")
        f3.pack(fill="x", **pad)
        ttk.Entry(f3, textvariable=self.sources_path).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f3, text="Chọn file .txt", command=self._pick_sources).pack(side="left", padx=6)

        # 4) Drive folder
        f4 = ttk.LabelFrame(root, text="4) Folder Drive lưu ảnh (link hoặc 'root' = My Drive)")
        f4.pack(fill="x", **pad)
        ttk.Entry(f4, textvariable=self.drive_folder).pack(fill="x", padx=6, pady=6)

        # 5) Nút chạy
        f5 = ttk.Frame(root)
        f5.pack(fill="x", **pad)
        self.btn_run = ttk.Button(f5, text="▶ BẮT ĐẦU CHẠY", command=self._run)
        self.btn_run.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(f5, text="Dừng", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.lbl_status = ttk.Label(root, text="Sẵn sàng.", foreground="#1a7f37")
        self.lbl_status.pack(anchor="w", padx=12)

        # Log
        ttk.Label(root, text="Nhật ký:").pack(anchor="w", padx=12, pady=(6, 0))
        logf = ttk.Frame(root)
        logf.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.log = tk.Text(logf, height=12, state="disabled", wrap="word",
                           bg="#111", fg="#ddd", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._init_license()
        self._autofill_defaults()
        self.root.after(120, self._poll)

    # ── Khởi tạo ────────────────────────────────────────────────────────────────
    def _init_license(self):
        try:
            from services.chichbong_imagen_service import resolve_license_from_client
            key = resolve_license_from_client()
        except Exception as e:
            key = ""
            self._log(f"Lỗi nạp engine: {e}")
        if key:
            self.lbl_license.config(text=f"License imagen4: {key[:8]}…{key[-4:]} (đọc từ máy)",
                                    foreground="#1a7f37")
        else:
            self.lbl_license.config(
                text="⚠ Không thấy license imagen4. Cài & kích hoạt app ChichBong trên máy này.",
                foreground="#b00020")

    def _autofill_defaults(self):
        # Gợi ý file mặc định nếu có sẵn cạnh app.
        for name, var in (("credentials.json", self.cred_path), ("sheet_sources.txt", self.sources_path)):
            p = APP_DIR / name
            if p.exists() and not var.get():
                var.set(str(p))
        if (APP_DIR / "token.json").exists():
            self.logged_in = True
            self.lbl_login.config(text="● Đã có token", foreground="#1a7f37")

    # ── Chọn file ────────────────────────────────────────────────────────────────
    def _pick_cred(self):
        f = filedialog.askopenfilename(title="Chọn credentials .json",
                                       filetypes=[("JSON", "*.json"), ("Tất cả", "*.*")])
        if f:
            self.cred_path.set(f)

    def _pick_sources(self):
        f = filedialog.askopenfilename(title="Chọn file danh sách sheet .txt",
                                       filetypes=[("Text", "*.txt"), ("Tất cả", "*.*")])
        if f:
            self.sources_path.set(f)

    # ── Đăng nhập Google ─────────────────────────────────────────────────────────
    def _login(self):
        cred = self.cred_path.get().strip()
        if not cred or not Path(cred).exists():
            messagebox.showwarning("Thiếu credentials", "Hãy chọn file credentials .json trước.")
            return
        self.btn_login.config(state="disabled", text="Đang đăng nhập…")
        self.lbl_login.config(text="● Đang mở trình duyệt…", foreground="#0b66c3")
        threading.Thread(target=self._login_worker, args=(cred,), daemon=True).start()

    def _login_worker(self, cred: str):
        old = sys.stdout
        sys.stdout = _QueueWriter(self.q)
        try:
            from services.sheet_drive_flow_service import _build_google_services
            _build_google_services(Path(cred), Path(self.token_path))
            self.q.put(("login_ok", None))
        except Exception as e:
            self.q.put(("login_err", str(e)))
        finally:
            sys.stdout = old

    # ── Chạy pipeline ────────────────────────────────────────────────────────────
    def _run(self):
        if self.running:
            return
        cred = self.cred_path.get().strip()
        src = self.sources_path.get().strip()
        if not cred or not Path(cred).exists():
            messagebox.showwarning("Thiếu credentials", "Hãy chọn file credentials .json.")
            return
        if not self.logged_in:
            messagebox.showwarning("Chưa đăng nhập", "Hãy bấm 'Đăng nhập Google' trước.")
            return
        if not src or not Path(src).exists():
            messagebox.showwarning("Thiếu danh sách", "Hãy chọn file .txt chứa link sheet.")
            return
        try:
            items = _parse_sources(src)
        except Exception as e:
            messagebox.showerror("Lỗi đọc file", str(e))
            return
        if not items:
            messagebox.showwarning("Trống", "File .txt không có link sheet hợp lệ.")
            return

        drive = self.drive_folder.get().strip() or "root"
        self.running = True
        self.stop_flag = False
        self.btn_run.config(state="disabled", text="Đang chạy…")
        self.btn_stop.config(state="normal")
        self.lbl_status.config(text=f"Đang chạy {len(items)} sheet…", foreground="#0b66c3")
        self._log(f"=== Bắt đầu: {len(items)} sheet | Drive={drive} ===")
        threading.Thread(target=self._run_worker, args=(items, cred, drive), daemon=True).start()

    def _run_worker(self, items, cred, drive):
        old = sys.stdout
        sys.stdout = _QueueWriter(self.q)
        import logging
        handler = logging.StreamHandler(_QueueWriter(self.q))  # type: ignore[arg-type]
        eng_logger = logging.getLogger("services.chichbong_imagen_service")
        eng_logger.addHandler(handler)
        try:
            from services.sheet_drive_flow_service import (
                SheetFlowConfig, run_sheet_drive_flow_pipeline, _extract_drive_folder_id,
            )
            parent = "root" if drive == "root" else (_extract_drive_folder_id(drive) or drive)
            total_ok = total_fail = 0
            for i, (sheet, mode) in enumerate(items, 1):
                if self.stop_flag:
                    self.q.put(("log", "■ Đã dừng theo yêu cầu."))
                    break
                self.q.put(("status", f"Sheet {i}/{len(items)} — {sheet[:48]}"))
                self.q.put(("log", f"\n--- Sheet {i}/{len(items)} ({mode}) ---"))
                try:
                    cfg = SheetFlowConfig(
                        sheet=sheet,
                        credentials=cred,
                        token_file=self.token_path,
                        drive_output_parent_id=parent,
                        generator="imagen4",
                    )
                    res = run_sheet_drive_flow_pipeline(cfg)
                    ok, fail = int(res.get("ok", 0)), int(res.get("fail", 0))
                    total_ok += ok
                    total_fail += fail
                    self.q.put(("log", f"✓ Sheet {i}: thành công {ok} dòng, lỗi {fail} dòng."))
                except Exception as e:
                    total_fail += 1
                    self.q.put(("log", f"✗ Sheet {i} lỗi: {e}"))
            self.q.put(("done", (total_ok, total_fail)))
        except Exception:
            self.q.put(("error", traceback.format_exc()))
        finally:
            sys.stdout = old
            eng_logger.removeHandler(handler)

    def _stop(self):
        self.stop_flag = True
        self.lbl_status.config(text="Đang dừng sau sheet hiện tại…", foreground="#b06000")
        self.btn_stop.config(state="disabled")

    # ── Poll hàng đợi ────────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self.lbl_status.config(text=str(data), foreground="#0b66c3")
                elif kind == "login_ok":
                    self.logged_in = True
                    self.btn_login.config(state="normal", text="2) Đăng nhập Google")
                    self.lbl_login.config(text="● Đã đăng nhập", foreground="#1a7f37")
                    self._log("✓ Đăng nhập Google thành công.")
                elif kind == "login_err":
                    self.btn_login.config(state="normal", text="2) Đăng nhập Google")
                    self.lbl_login.config(text="● Lỗi đăng nhập", foreground="#b00020")
                    self._log(f"✗ Lỗi đăng nhập: {data}")
                    messagebox.showerror("Lỗi đăng nhập", str(data))
                elif kind == "done":
                    self._finish_done(data)
                elif kind == "error":
                    self._finish_error(data)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _finish_done(self, data):
        self.running = False
        self.btn_run.config(state="normal", text="▶ BẮT ĐẦU CHẠY")
        self.btn_stop.config(state="disabled")
        ok, fail = data if data else (0, 0)
        self.lbl_status.config(text=f"✓ Xong: {ok} dòng thành công, {fail} lỗi.", foreground="#1a7f37")
        self._log(f"=== HOÀN TẤT: {ok} thành công, {fail} lỗi ===")

    def _finish_error(self, msg):
        self.running = False
        self.btn_run.config(state="normal", text="▶ BẮT ĐẦU CHẠY")
        self.btn_stop.config(state="disabled")
        self.lbl_status.config(text="✗ Lỗi — xem nhật ký.", foreground="#b00020")
        self._log(f"LỖI:\n{msg}")
        messagebox.showerror("Lỗi", str(msg)[:1000])

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
