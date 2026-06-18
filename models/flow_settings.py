from dataclasses import dataclass
from typing import Optional

@dataclass
class FlowSettings:
    """Cấu hình UI cho Google Flow, dùng để apply trước khi tạo ảnh."""
    auto_apply: bool = True
    top_mode: str = "image"
    secondary_mode: str = ""
    aspect_ratio: str = "16:9"
    multiplier: str = "x1"
    model_name: str = "Nano Banana 2"
    allow_model_alias_fallback: bool = False
