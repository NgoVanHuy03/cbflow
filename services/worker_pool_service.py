import asyncio
import os
import re
import time
from typing import List, Callable
from playwright.async_api import async_playwright

from models.worker_config import WorkerConfig
from models.image_job import ImageJob

class WorkerPool:
    """
    Quản lý pool các worker Chrome chạy song song. 
    Chia Job vào Queue để tận dụng tối đa tài nguyên rảnh rỗi.
    """
    def __init__(self, configs: List[WorkerConfig]):
        self.configs = configs
        self.contexts = {} # worker_id -> browser_context
        self.playwright = None
        # Trang chủ Google Flow (dashboard), KHÔNG cố định project id.
        self.flow_home_url = "https://labs.google/fx/vi/tools/flow"

    def _is_process_alive(self, pid: int) -> bool:
        """
        Kiểm tra PID còn sống không.
        - Dùng os.kill(pid, 0): không gửi signal thật, chỉ kiểm tra tồn tại process.
        """
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Có process nhưng user hiện tại không đủ quyền.
            return True
        except Exception:
            return False

    def _cleanup_stale_profile_locks(self, profile_dir: str) -> None:
        """
        Dọn lock mồ côi trong Chrome profile nếu lần chạy trước bị crash.
        Tại sao cần:
        - Profile persistent có các symlink SingletonLock/Socket/Cookie.
        - Nếu process cũ chết bất thường, lock còn sót lại -> Playwright mở browser xong tự thoát.
        """
        lock_path = os.path.join(profile_dir, "SingletonLock")
        cookie_path = os.path.join(profile_dir, "SingletonCookie")
        socket_path = os.path.join(profile_dir, "SingletonSocket")

        # Chỉ xử lý khi lock tồn tại; nếu không có lock thì không cần động vào profile.
        if not os.path.lexists(lock_path):
            return

        # SingletonLock thường có dạng symlink "HOSTNAME-<pid>".
        # Nếu parse được pid và process còn sống -> giữ nguyên lock.
        # Nếu pid đã chết/không parse được -> coi là lock mồ côi và dọn.
        should_cleanup = True
        try:
            raw = os.readlink(lock_path)
            pid_str = str(raw).rsplit("-", 1)[-1]
            pid = int(pid_str)
            if self._is_process_alive(pid):
                should_cleanup = False
        except Exception:
            should_cleanup = True

        if not should_cleanup:
            return

        for p in (lock_path, cookie_path, socket_path):
            try:
                if os.path.lexists(p):
                    os.unlink(p)
            except Exception:
                # Nếu xóa lock thất bại, để Playwright tự báo lỗi launch chi tiết.
                pass

    async def start_all(self):
        """Khởi động tất cả Playwright Context."""
        self.playwright = await async_playwright().start()
        
        for cfg in self.configs:
            print(f"[*] Bật Chrome {cfg.worker_id} | Profile: {cfg.profile_dir}")
            # Dọn lock profile mồ côi trước khi launch để tránh browser tự thoát ngay.
            self._cleanup_stale_profile_locks(cfg.profile_dir)
            launch_args = {
                "user_data_dir": cfg.profile_dir,
                "headless": False,
                # Dùng Chrome system thay vì Chromium bundle để ổn định profile đăng nhập hiện có.
                "channel": "chrome",
                "args": [
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                "ignore_default_args": ["--enable-automation"],
                "accept_downloads": True,
            }
            if cfg.proxy:
                print(f"    -> Gắn Proxy: {cfg.proxy}")
                launch_args["proxy"] = {"server": cfg.proxy}
            
            context = await self.playwright.chromium.launch_persistent_context(**launch_args)
            self.contexts[cfg.worker_id] = context

    async def stop_all(self):
        """Đóng tất cả trình duyệt."""
        for worker_id, context in self.contexts.items():
            try:
                await context.close()
                print(f"[*] Đã đóng Chrome {worker_id}")
            except Exception as exc:
                # Có thể context đã rớt kết nối trước đó (ví dụ user Ctrl+C).
                # Không coi là lỗi nghiệp vụ tải ảnh, chỉ log để biết.
                print(f"[WARN] Không đóng được context {worker_id}: {exc}")
        
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception as exc:
                print(f"[WARN] Không stop được Playwright: {exc}")

    async def _close_all_existing_tabs(self, context) -> None:
        """
        Đóng toàn bộ tab hiện có trong context.

        Vì sao cần:
        - Tránh mang state cũ (tab scene/project cũ) sang job mới.
        - Luồng của bạn yêu cầu luôn bắt đầu từ 1 tab mới sạch.
        """
        pages = list(context.pages)
        for p in pages:
            try:
                await p.close()
            except Exception:
                # Nếu tab đã tự đóng thì bỏ qua.
                pass

    async def _is_flow_editor_ready(self, page, timeout_sec: int = 20) -> bool:
        """
        Kiểm tra đã vào được editor chưa (có ô nhập prompt).

        Request kiểm tra:
        - Tìm element contenteditable/role=textbox/textarea.
        Response mong muốn:
        - Có ít nhất 1 ô nhập hiển thị -> True.
        """
        selectors = [
            "div[contenteditable='true']",
            "div[role='textbox']",
            "textarea",
        ]
        start = time.time()
        while time.time() - start < timeout_sec:
            for sel in selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible(timeout=250):
                        return True
                except Exception:
                    continue
            await asyncio.sleep(0.6)
        return False

    async def _open_flow_new_project_tab(self, context, worker_id: str):
        """
        Tạo 1 tab mới và ép tạo project mới trên Google Flow.

        Luồng:
        1) Mở tab mới -> goto trang chủ Flow.
        2) Tìm và click nút "Dự án mới / New project / Create project".
        3) Chờ URL chuyển sang /project/... và editor sẵn sàng.
        4) Nếu không tạo được project mới thì fallback dùng editor hiện tại.
        """
        page = await context.new_page()
        print(f"[*] [{worker_id}] Mở tab mới vào Flow home: {self.flow_home_url}")
        await page.goto(self.flow_home_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.5)

        # Nếu vào thẳng editor luôn (do account state), dùng luôn.
        if await self._is_flow_editor_ready(page, timeout_sec=3):
            print(f"[*] [{worker_id}] Editor đã sẵn sàng ngay, dùng tab hiện tại.")
            return page

        project_patterns = [
            re.compile(r"dự án mới", re.IGNORECASE),
            re.compile(r"new project", re.IGNORECASE),
            re.compile(r"create project", re.IGNORECASE),
            re.compile(r"tạo dự án", re.IGNORECASE),
        ]

        start = time.time()
        while time.time() - start < 35:
            clicked = False
            for patt in project_patterns:
                try:
                    btn = page.get_by_text(patt).first
                    if await btn.count() > 0 and await btn.is_visible(timeout=300):
                        print(f"[*] [{worker_id}] Click nút tạo project mới: /{patt.pattern}/")
                        await btn.click()
                        clicked = True
                        await asyncio.sleep(2.0)
                        break
                except Exception:
                    continue

            if not clicked:
                # Fallback theo button chứa text liên quan tạo project.
                try:
                    btn2 = page.locator("button").filter(
                        has_text=re.compile(
                            r"(dự án mới|new project|create project|tạo dự án)",
                            re.IGNORECASE,
                        )
                    ).first
                    if await btn2.count() > 0 and await btn2.is_visible(timeout=300):
                        print(f"[*] [{worker_id}] Click fallback button tạo project mới.")
                        await btn2.click()
                        clicked = True
                        await asyncio.sleep(2.0)
                except Exception:
                    pass

            if "/project/" in (page.url or ""):
                ready = await self._is_flow_editor_ready(page, timeout_sec=15)
                if ready:
                    print(f"[*] [{worker_id}] Đã vào project mới: {page.url}")
                    return page

            # Nếu chưa click được hoặc chưa chuyển URL thì chờ ngắn rồi thử lại.
            await asyncio.sleep(0.8)

        # Fallback cuối: không tạo được project mới nhưng vẫn thử dùng editor hiện tại.
        if await self._is_flow_editor_ready(page, timeout_sec=8):
            print(f"[WARN] [{worker_id}] Không ép tạo project mới được, fallback editor hiện tại.")
            return page

        raise RuntimeError("Không vào được editor Google Flow sau khi mở tab mới.")

    async def _worker_loop(self, worker_id: str, queue: asyncio.Queue, generate_func: Callable):
        """Vòng lặp vô tận của 1 worker: hễ rảnh là nhặt Job từ Queue."""
        context = self.contexts[worker_id]
        
        while True:
            job: ImageJob = await queue.get()
            if job is None:
                # Tín hiệu None = Đã hết việc, worker xin phép nghỉ
                queue.task_done()
                break
                
            print(f"\n[🚀 {worker_id}] Đang thực hiện kịch bản: {job.job_id}")
            page = None
            try:
                # Reset trạng thái lỗi/result cho job trước mỗi lần chạy.
                job.metadata["worker_error"] = None
                job.metadata["worker_result_count"] = 0
                job.metadata["worker_last_id"] = worker_id

                # Bước 1 theo yêu cầu:
                # - Đóng hết tab cũ.
                # - Mở 1 tab mới vào trang chủ Flow.
                # - Tạo project mới trước khi chạy prompt.
                await self._close_all_existing_tabs(context)
                page = await self._open_flow_new_project_tab(context, worker_id)
                
                # Uỷ quyền toàn bộ việc thao tác UI cho Core Engine
                results = await generate_func(page, job)
                job.metadata["worker_result_count"] = int(len(results or []))
                print(f"[✅ {worker_id}] Hoàn thành {job.job_id} | Ra được {len(results)} ảnh.")
                
            except Exception as e:
                job.metadata["worker_error"] = {
                    "type": e.__class__.__name__,
                    "message": str(e),
                    "worker_id": worker_id,
                }
                print(f"[❌ {worker_id}] Lỗi khi chạy {job.job_id}: {str(e)}")
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                queue.task_done()

    async def run_jobs_parallel(self, jobs: List[ImageJob], generate_func: Callable) -> None:
        """
        Tạo hàng đợi (Queue) và chia đều công việc cho các Chrome cày song song.
        """
        queue = asyncio.Queue()
        
        # 1. Đưa tất cả Job vào Queue
        for job in jobs:
            queue.put_nowait(job)
            
        # 2. Đưa "thẻ báo nghỉ" (None) vào cuối Queue cho từng worker
        for _ in self.configs:
            queue.put_nowait(None)
            
        print(f"\n=============================================")
        print(f"🚀 BẮT ĐẦU CHẠY {len(jobs)} KỊCH BẢN BẰNG {len(self.configs)} CHROME")
        print(f"=============================================")

        # 3. Ra lệnh cho toàn bộ worker cùng nhảy vào Queue tranh việc
        tasks = []
        for cfg in self.configs:
            task = asyncio.create_task(self._worker_loop(cfg.worker_id, queue, generate_func))
            tasks.append(task)
            
        # 4. Chờ cho đến khi worker cuối cùng làm xong việc cuối cùng
        await asyncio.gather(*tasks)
