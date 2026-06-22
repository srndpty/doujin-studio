from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///data/local-doujin-studio.db"
    export_dir: Path = Path("exports")
    knowledge_dir: Path = Path("data/knowledge")
    image_backend: str = "stub"
    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_workflow_path: Path = Path("workflows/default.workflow_api.json")
    comfyui_timeout_seconds: float = 120.0
    comfyui_positive_node_id: str = "6"
    comfyui_negative_node_id: str = "7"
    comfyui_seed_node_id: str = "3"
    comfyui_width_node_id: str = "5"
    comfyui_height_node_id: str = "5"
    comfyui_save_prefix_node_id: str = "9"
    # 接続不可・タイムアウトなど一時障害のときだけstub画像へ退避する。
    # ワークフロー不正やノード不足などの設定不備は退避せずエラーにする。
    comfyui_fallback_to_stub: bool = True
    llm_provider: str = "stub"
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    llm_model: str = ""
    llm_timeout_seconds: float = 180.0
    llm_json_mode: str = "auto"
    llm_max_context_chars: int = 24000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///data/local-doujin-studio.db"),
            export_dir=Path(os.getenv("EXPORT_DIR", "exports")),
            knowledge_dir=Path(os.getenv("KNOWLEDGE_DIR", "data/knowledge")),
            image_backend=os.getenv("IMAGE_BACKEND", "stub"),
            comfyui_base_url=os.getenv("COMFYUI_BASE_URL", "http://127.0.0.1:8188"),
            comfyui_workflow_path=Path(
                os.getenv("COMFYUI_WORKFLOW_PATH", "workflows/default.workflow_api.json")
            ),
            comfyui_timeout_seconds=float(os.getenv("COMFYUI_TIMEOUT_SECONDS", "120")),
            comfyui_positive_node_id=os.getenv("COMFYUI_POSITIVE_NODE_ID", "6"),
            comfyui_negative_node_id=os.getenv("COMFYUI_NEGATIVE_NODE_ID", "7"),
            comfyui_seed_node_id=os.getenv("COMFYUI_SEED_NODE_ID", "3"),
            comfyui_width_node_id=os.getenv("COMFYUI_WIDTH_NODE_ID", "5"),
            comfyui_height_node_id=os.getenv("COMFYUI_HEIGHT_NODE_ID", "5"),
            comfyui_save_prefix_node_id=os.getenv("COMFYUI_SAVE_PREFIX_NODE_ID", "9"),
            comfyui_fallback_to_stub=os.getenv("COMFYUI_FALLBACK_TO_STUB", "true").lower()
            not in {"0", "false", "no"},
            llm_provider=os.getenv("LLM_PROVIDER", "stub"),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
            llm_model=os.getenv("LLM_MODEL", ""),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
            llm_json_mode=os.getenv("LLM_JSON_MODE", "auto"),
            llm_max_context_chars=int(os.getenv("LLM_MAX_CONTEXT_CHARS", "24000")),
        )
