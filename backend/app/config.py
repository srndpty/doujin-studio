from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///data/local-doujin-studio.db"
    export_dir: Path = Path("exports")
    image_backend: str = "stub"
    comfyui_base_url: str = "http://127.0.0.1:8188"
    llm_provider: str = "stub"
    llm_base_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///data/local-doujin-studio.db"),
            export_dir=Path(os.getenv("EXPORT_DIR", "exports")),
            image_backend=os.getenv("IMAGE_BACKEND", "stub"),
            comfyui_base_url=os.getenv("COMFYUI_BASE_URL", "http://127.0.0.1:8188"),
            llm_provider=os.getenv("LLM_PROVIDER", "stub"),
            llm_base_url=os.getenv("LLM_BASE_URL", ""),
        )
