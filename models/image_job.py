from dataclasses import dataclass, field
from typing import List, Optional
from models.flow_settings import FlowSettings

@dataclass
class ImageJob:
    """Mô tả 1 nhiệm vụ tạo ảnh độc lập."""
    job_id: str
    prompts: List[str]
    output_dir: str
    reference_images: Optional[List[str]] = None
    settings: Optional[FlowSettings] = None
    metadata: dict = field(default_factory=dict)
