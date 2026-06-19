#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Giao diện chạy pipeline: Google Sheet → tạo ảnh (imagen4) → upload Drive → ghi link về Sheet.

Trên giao diện:
  1. Chọn file credentials (.json) — chìa khóa Google API để đọc/ghi sheet.
  2. Đăng nhập Google (mở trình duyệt; lưu token.json → lần sau KHỎI đăng nhập lại).
  3. Chọn file danh sách sheet (.txt) — mỗi dòng 1 link sheet (có thể kèm |hoa).
  4. Chọn tỉ lệ + chất lượng ảnh, folder Drive lưu ảnh.
  5. Bấm BẮT ĐẦU CHẠY.

Cấu hình (file/tỉ lệ/Drive) tự LƯU lại → lần sau mở lên đã điền sẵn.
License imagen4 tự đọc từ Registry máy (cần app ChichBong đã kích hoạt).

Chạy:  python giao_dien_sheet.py
"""

from __future__ import annotations

import json
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
TOKEN_PATH = APP_DIR / "token.json"
CONFIG_PATH = APP_DIR / "cau_hinh_sheet.json"   # nơi lưu cấu hình lần trước

RATIO_MAP = {"Vuông (1:1)": "square", "Ngang (16:9)": "landscape", "Dọc (9:16)": "portrait"}
QUALITY_MAP = {"2K (nét hơn)": "2k", "1K (nhanh hơn)": "1k"}


class _QueueWriter:
    """Chuyển print() của pipeline vào hàng đợi GUI."""
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


def _key_for_value(d: dict, value: str, default_idx: int = 0) -> str:
    for k, v in d.items():
        if v == value:
            return k
    return list(d.keys())[default_idx]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        self.running = False
        self.stop_flag = False
        self.logged_in = False

        cfg = self._load_config()
        self.cred_path = tk.StringVar(value=cfg.get("credentials", ""))
        self.sources_path = tk.StringVar(value=cfg.get("sources", ""))
        self.drive_folder = tk.StringVar(value=cfg.get("drive_folder", "root"))
        self.var_ratio = tk.StringVar(value=_key_for_value(RATIO_MAP, cfg.get("ratio", "square")))
        self.var_quality = tk.StringVar(value=_key_for_value(QUALITY_MAP, cfg.get("quality", "2k")))
        self.var_in_flight = tk.StringVar(value=str(max(1, min(100, int(cfg.get("in_flight", 6) or 6)))))

        root.title("Chạy Sheet → Ảnh → Drive (ChichBong Imagen4)")
        root.geometry("700x700")
        root.minsize(620, 640)
        pad = {"padx": 12, "pady": 4}

        ttk.Label(root, text="Chạy nhiều Sheet tự động",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w", **pad)
        self.lbl_license = ttk.Label(root, text="Đang kiểm tra license…", foreground="#555")
        self.lbl_license.pack(anchor="w", padx=12)

        # 1) Credentials
        f1 = ttk.LabelFrame(root, text="1) File credentials (.json)")
        f1.pack(fill="x", **pad)
        ttk.Entry(f1, textvariable=self.cred_path).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f1, text="Chọn file .json", command=self._pick_cred).pack(side="left", padx=6)

        # 2) Đăng nhập
        f2 = ttk.Frame(root)
        f2.pack(fill="x", **pad)
        self.btn_login = ttk.Button(f2, text="2) Đăng nhập Google", command=self._login)
        self.btn_login.pack(side="left")
        self.lbl_login = ttk.Label(f2, text="● Chưa đăng nhập", foreground="#b00020")
        self.lbl_login.pack(side="left", padx=10)

        # 3) Sources
        f3 = ttk.LabelFrame(root, text="3) File danh sách sheet (.txt)")
        f3.pack(fill="x", **pad)
        ttk.Entry(f3, textvariable=self.sources_path).pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(f3, text="Chọn file .txt", command=self._pick_sources).pack(side="left", padx=6)

        # 4) Tuỳ chọn ảnh + Drive
        f4 = ttk.LabelFrame(root, text="4) Tuỳ chọn ảnh & nơi lưu Drive")
        f4.pack(fill="x", **pad)
        row = ttk.Frame(f4)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Label(row, text="Tỉ lệ:").pack(side="left")
        ttk.Combobox(row, textvariable=self.var_ratio, values=list(RATIO_MAP.keys()),
                     state="readonly", width=12).pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Chất lượng:").pack(side="left")
        ttk.Combobox(row, textvariable=self.var_quality, values=list(QUALITY_MAP.keys()),
                     state="readonly", width=12).pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Gửi cùng lúc:").pack(side="left")
        ttk.Entry(row, textvariable=self.var_in_flight, width=5).pack(side="left", padx=4)
        ttk.Label(row, text="(gõ số 1–100, khuyên 15–20)", foreground="#888").pack(side="left")
        row2 = ttk.Frame(f4)
        row2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(row2, text="Folder Drive (link hoặc 'root' = My Drive):").pack(side="left")
        ttk.Entry(row2, textvariable=self.drive_folder).pack(side="left", fill="x", expand=True, padx=4)

        # 5) Chạy
        f5 = ttk.Frame(root)
        f5.pack(fill="x", **pad)
        self.btn_run = ttk.Button(f5, text="▶ BẮT ĐẦU CHẠY", command=self._run)
        self.btn_run.pack(side="left", fill="x", expand=True)
        self.btn_stop = ttk.Button(f5, text="Dừng", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        self.lbl_status = ttk.Label(root, text="Sẵn sàng.", foreground="#1a7f37")
        self.lbl_status.pack(anchor="w", padx=12)

        # Nơi lưu
        self.lbl_saveinfo = ttk.Label(root, text="", foreground="#666", font=("Segoe UI", 8))
        self.lbl_saveinfo.pack(anchor="w", padx=12)

        ttk.Label(root, text="Nhật ký:").pack(anchor="w", padx=12, pady=(6, 0))
        logf = ttk.Frame(root)
        logf.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.log = tk.Text(logf, height=11, state="disabled", wrap="word",
                           bg="#111", fg="#ddd", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._init_license()
        self._refresh_login_state()
        self._refresh_saveinfo()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._poll)

    def _get_in_flight(self) -> int:
        """Đọc ô 'Gửi cùng lúc' an toàn (gõ sai → mặc định 6), giới hạn 1–100."""
        try:
            n = int(str(self.var_in_flight.get()).strip() or "6")
        except Exception:
            n = 6
        return max(1, min(100, n))

    # ── Cấu hình (lưu/đọc) ───────────────────────────────────────────────────────
    def _load_config(self) -> dict:
        try:
            if CONFIG_PATH.exists():
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps({
                "credentials": self.cred_path.get().strip(),
                "sources": self.sources_path.get().strip(),
                "drive_folder": self.drive_folder.get().strip() or "root",
                "ratio": RATIO_MAP.get(self.var_ratio.get(), "square"),
                "quality": QUALITY_MAP.get(self.var_quality.get(), "2k"),
                "in_flight": self._get_in_flight(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self):
        self._save_config()
        self.root.destroy()

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

    def _refresh_login_state(self):
        if TOKEN_PATH.exists():
            self.logged_in = True
            self.lbl_login.config(text="● Đã đăng nhập (dùng token đã lưu)", foreground="#1a7f37")
        else:
            self.logged_in = False
            self.lbl_login.config(text="● Chưa đăng nhập", foreground="#b00020")

    def _refresh_saveinfo(self):
        token_txt = "đã lưu — lần sau khỏi đăng nhập lại" if TOKEN_PATH.exists() else "chưa có"
        self.lbl_saveinfo.config(
            text=(f"Đăng nhập lưu tại: {TOKEN_PATH.name} ({token_txt})   |   "
                  f"Cấu hình lưu tại: {CONFIG_PATH.name}   |   Thư mục: {APP_DIR}"))

    # ── Chọn file ────────────────────────────────────────────────────────────────
    def _pick_cred(self):
        f = filedialog.askopenfilename(title="Chọn credentials .json",
                                       filetypes=[("JSON", "*.json"), ("Tất cả", "*.*")])
        if f:
            self.cred_path.set(f)
            self._save_config()

    def _pick_sources(self):
        f = filedialog.askopenfilename(title="Chọn file danh sách sheet .txt",
                                       filetypes=[("Text", "*.txt"), ("Tất cả", "*.*")])
        if f:
            self.sources_path.set(f)
            self._save_config()

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
            _build_google_services(Path(cred), TOKEN_PATH)
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

        self._save_config()  # lưu lựa chọn cho lần sau
        drive = self.drive_folder.get().strip() or "root"
        ratio = RATIO_MAP.get(self.var_ratio.get(), "square")
        quality = QUALITY_MAP.get(self.var_quality.get(), "2k")
        in_flight = self._get_in_flight()

        self.running = True
        self.stop_flag = False
        self.btn_run.config(state="disabled", text="Đang chạy…")
        self.btn_stop.config(state="normal")
        self.lbl_status.config(text=f"Đang chạy {len(items)} sheet…", foreground="#0b66c3")
        self._log(f"=== Bắt đầu: {len(items)} sheet | tỉ lệ={ratio} | {quality.upper()} | "
                  f"gửi cùng lúc={in_flight} | Drive={drive} ===")
        threading.Thread(target=self._run_worker,
                         args=(items, cred, drive, ratio, quality, in_flight), daemon=True).start()

    def _run_worker(self, items, cred, drive, ratio, quality, in_flight):
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
                        token_file=str(TOKEN_PATH),
                        drive_output_parent_id=parent,
                        generator="imagen4",
                        imagen4_aspect_ratio=ratio,
                        imagen4_quality=quality,
                        imagen4_max_in_flight=in_flight,
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

    # ── Poll ─────────────────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self.lbl_status.config(text=str(data), foreground="#0b66c3")
                elif kind == "login_ok":
                    self.btn_login.config(state="normal", text="2) Đăng nhập Google")
                    self._refresh_login_state()
                    self._refresh_saveinfo()
                    self._log("✓ Đăng nhập Google thành công — token đã lưu.")
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
