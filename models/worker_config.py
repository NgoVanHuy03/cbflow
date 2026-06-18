from dataclasses import dataclass
from typing import Optional

@dataclass
class WorkerConfig:
    """Cấu hình cho 1 worker/trình duyệt Chrome."""
    worker_id: str
    profile_dir: str
    proxy: Optional[str] = None
    proxy_real: Optional[str] = None
