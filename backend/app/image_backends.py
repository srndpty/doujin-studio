from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image, ImageDraw, ImageFont

from .schemas import Panel


@dataclass(frozen=True)
class ImageResult:
    backend: str
    status: str
    asset_path: Path | None
    message: str


class ImageBackend(Protocol):
    async def generate_panel(self, project_id: str, panel: Panel, export_dir: Path) -> ImageResult:
        pass


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/YuGothM.ttc"),
        Path("C:/Windows/Fonts/YuGothR.ttc"),
        Path("C:/Windows/Fonts/msgothic.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


class StubImageBackend:
    async def generate_panel(self, project_id: str, panel: Panel, export_dir: Path) -> ImageResult:
        panel_dir = export_dir / project_id / "panels"
        panel_dir.mkdir(parents=True, exist_ok=True)
        target = panel_dir / f"{panel.panel_id}.png"
        digest = hashlib.sha256(panel.panel_id.encode("utf-8")).digest()
        base = (220 + digest[0] % 24, 224 + digest[1] % 20, 230 + digest[2] % 18)
        accent = (70 + digest[3] % 90, 80 + digest[4] % 90, 90 + digest[5] % 90)

        image = Image.new("RGB", (768, 512), base)
        draw = ImageDraw.Draw(image)
        font_large = load_font(36)
        font_small = load_font(22)
        draw.rectangle((24, 24, 744, 488), outline=accent, width=6)
        draw.text((48, 48), panel.panel_id, fill=accent, font=font_large)
        draw.text((48, 108), panel.shot, fill=(40, 40, 40), font=font_small)
        for line_index, line in enumerate(wrap_text(panel.prompt, 28)[:5]):
            draw.text((48, 160 + line_index * 32), line, fill=(55, 55, 55), font=font_small)
        image.save(target)
        return ImageResult("stub", "done", target, "stub画像を生成しました")


class ComfyUIImageBackend:
    def __init__(self, base_url: str, fallback: StubImageBackend | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.fallback = fallback or StubImageBackend()

    async def generate_panel(self, project_id: str, panel: Panel, export_dir: Path) -> ImageResult:
        payload = {
            "prompt": {},
            "client_id": f"local-doujin-studio-{project_id}",
            "extra_data": {
                "panel_id": panel.panel_id,
                "positive_prompt": panel.generation.prompt or panel.prompt,
                "negative_prompt": panel.generation.negative_prompt,
                "seed": panel.generation.seed,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.post(f"{self.base_url}/prompt", content=json.dumps(payload))
                response.raise_for_status()
        except Exception as exc:
            fallback = await self.fallback.generate_panel(project_id, panel, export_dir)
            return ImageResult("comfyui", "fallback", fallback.asset_path, f"ComfyUIへ接続できないためstubに戻しました: {exc}")
        fallback = await self.fallback.generate_panel(project_id, panel, export_dir)
        return ImageResult("comfyui", "queued", fallback.asset_path, "ComfyUIへキュー投入し、MVP表示用stubも生成しました")


def build_image_backend(name: str, comfyui_base_url: str) -> ImageBackend:
    if name.lower() == "comfyui":
        return ComfyUIImageBackend(comfyui_base_url)
    return StubImageBackend()


def wrap_text(text: str, width: int) -> list[str]:
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        current += char
        if len(current) >= width:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines
