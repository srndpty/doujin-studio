from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

import httpx
from PIL import Image, ImageDraw, ImageFont
from websockets.asyncio.client import ClientConnection, connect

from .assets import resolve_asset_path
from .config import Settings
from .schemas import Panel

NO_TEXT_PROMPT_SUFFIX = "no text, no speech bubble, no watermark, no manga panel text"


@dataclass(frozen=True)
class ImageResult:
    backend: str
    status: str
    asset_path: Path | None
    message: str
    prompt_id: str | None = None


@dataclass(frozen=True)
class ComfyUIWorkflowConfig:
    workflow_path: Path
    positive_node_id: str
    negative_node_id: str
    seed_node_id: str
    width_node_id: str
    height_node_id: str
    save_prefix_node_id: str


@dataclass(frozen=True)
class ComfyUIStatus:
    backend: str
    base_url: str
    workflow_path: str
    connected: bool
    workflow_exists: bool
    workflow_valid: bool
    missing_nodes: list[str]
    message: str


class ImageBackend(Protocol):
    async def generate_panel(
        self,
        project_id: str,
        panel: Panel,
        export_dir: Path,
        target_path: Path | None = None,
        progress_callback: Callable[[int, int, str | None, str], Awaitable[None]] | None = None,
    ) -> ImageResult:
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
    async def generate_panel(
        self,
        project_id: str,
        panel: Panel,
        export_dir: Path,
        target_path: Path | None = None,
        progress_callback: Callable[[int, int, str | None, str], Awaitable[None]] | None = None,
    ) -> ImageResult:
        panel_dir = export_dir / project_id / "panels"
        panel_dir.mkdir(parents=True, exist_ok=True)
        target = target_path or panel_dir / f"{panel.panel_id}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        if progress_callback:
            await progress_callback(0, 1, None, "stub画像を生成中です")
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
        if progress_callback:
            await progress_callback(1, 1, None, "stub画像を生成しました")
        return ImageResult("stub", "done", target, "stub画像を生成しました")


class ComfyUIImageBackend:
    def __init__(
        self,
        base_url: str,
        workflow_config: ComfyUIWorkflowConfig,
        timeout_seconds: float,
        fallback: StubImageBackend | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.workflow_config = workflow_config
        self.timeout_seconds = timeout_seconds
        self.fallback = fallback or StubImageBackend()

    async def generate_panel(
        self,
        project_id: str,
        panel: Panel,
        export_dir: Path,
        target_path: Path | None = None,
        progress_callback: Callable[[int, int, str | None, str], Awaitable[None]] | None = None,
    ) -> ImageResult:
        target = target_path or export_dir / project_id / "panels" / f"{panel.panel_id}.png"
        try:
            workflow = load_workflow(self.workflow_config)
            workflow = apply_panel_to_workflow(
                workflow=workflow,
                config=self.workflow_config,
                panel=panel,
                filename_prefix=f"local-doujin-studio/{project_id}/{panel.panel_id}",
            )
            client_id = f"local-doujin-studio-{project_id}-{panel.panel_id}-{uuid.uuid4()}"
            websocket = await open_comfyui_websocket(self.base_url, client_id)
            async with httpx.AsyncClient(timeout=10.0) as client:
                await apply_reference_images_to_workflow(
                    client,
                    self.base_url,
                    workflow,
                    panel,
                    export_dir,
                    project_id,
                )
                response = await client.post(
                    f"{self.base_url}/prompt",
                    json={"prompt": workflow, "client_id": client_id},
                )
                response.raise_for_status()
                prompt_id = response.json().get("prompt_id")
                if not prompt_id:
                    raise RuntimeError("ComfyUIからprompt_idが返りませんでした")
                if progress_callback:
                    await progress_callback(0, 1, None, "ComfyUIへ投入しました")
                if websocket:
                    try:
                        try:
                            await wait_for_websocket_completion(
                                websocket, prompt_id, self.timeout_seconds, progress_callback
                            )
                        except Exception:
                            if progress_callback:
                                await progress_callback(
                                    0, 1, None, "WebSocket監視を継続できないため履歴を確認します"
                                )
                    finally:
                        await websocket.close()
                history = await wait_for_history(
                    client, self.base_url, prompt_id, self.timeout_seconds
                )
                image_ref = find_first_image(history, prompt_id)
                image_response = await client.get(f"{self.base_url}/view", params=image_ref)
                image_response.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(image_response.content)
                return ImageResult(
                    "comfyui", "done", target, "ComfyUI画像を取得しました", prompt_id=prompt_id
                )
        except Exception as exc:
            fallback = await self.fallback.generate_panel(
                project_id,
                panel,
                export_dir,
                target_path=target,
                progress_callback=progress_callback,
            )
            return ImageResult(
                "comfyui",
                "fallback",
                fallback.asset_path,
                f"ComfyUI生成に失敗したためstubに戻しました: {exc}",
            )

    async def get_status(self) -> ComfyUIStatus:
        workflow_exists = self.workflow_config.workflow_path.exists()
        workflow_valid = False
        missing_nodes = validate_workflow_nodes(self.workflow_config)
        if workflow_exists and not missing_nodes:
            workflow_valid = True

        connected = False
        connect_message = ""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self.base_url}/system_stats")
                response.raise_for_status()
                connected = True
                connect_message = "ComfyUIへ接続できました"
        except Exception as exc:
            connect_message = f"ComfyUIへ接続できません: {exc}"

        if not workflow_exists:
            message = "workflow_api.jsonが見つかりません"
        elif missing_nodes:
            message = f"workflow設定ノードが不足しています: {', '.join(missing_nodes)}"
        else:
            message = connect_message

        return ComfyUIStatus(
            backend="comfyui",
            base_url=self.base_url,
            workflow_path=str(self.workflow_config.workflow_path),
            connected=connected,
            workflow_exists=workflow_exists,
            workflow_valid=workflow_valid,
            missing_nodes=missing_nodes,
            message=message,
        )


def build_image_backend(settings: Settings) -> ImageBackend:
    if settings.image_backend.lower() == "comfyui":
        return ComfyUIImageBackend(
            settings.comfyui_base_url,
            workflow_config=ComfyUIWorkflowConfig(
                workflow_path=settings.comfyui_workflow_path,
                positive_node_id=settings.comfyui_positive_node_id,
                negative_node_id=settings.comfyui_negative_node_id,
                seed_node_id=settings.comfyui_seed_node_id,
                width_node_id=settings.comfyui_width_node_id,
                height_node_id=settings.comfyui_height_node_id,
                save_prefix_node_id=settings.comfyui_save_prefix_node_id,
            ),
            timeout_seconds=settings.comfyui_timeout_seconds,
        )
    return StubImageBackend()


async def get_comfyui_status(settings: Settings) -> ComfyUIStatus:
    if settings.image_backend.lower() != "comfyui":
        return ComfyUIStatus(
            backend=settings.image_backend,
            base_url=settings.comfyui_base_url,
            workflow_path=str(settings.comfyui_workflow_path),
            connected=False,
            workflow_exists=settings.comfyui_workflow_path.exists(),
            workflow_valid=False,
            missing_nodes=[],
            message="IMAGE_BACKENDがstubのためComfyUIは使用しません",
        )
    backend = build_image_backend(settings)
    if isinstance(backend, ComfyUIImageBackend):
        return await backend.get_status()
    raise RuntimeError("ComfyUI backendを初期化できませんでした")


def load_workflow(config: ComfyUIWorkflowConfig) -> dict:
    if not config.workflow_path.exists():
        raise FileNotFoundError(f"workflow_api.jsonが見つかりません: {config.workflow_path}")
    with config.workflow_path.open("r", encoding="utf-8") as file:
        workflow = json.load(file)
    missing = validate_workflow_nodes(config, workflow)
    if missing:
        raise ValueError(f"workflow設定ノードが不足しています: {', '.join(missing)}")
    return workflow


def validate_workflow_nodes(
    config: ComfyUIWorkflowConfig, workflow: dict | None = None
) -> list[str]:
    if workflow is None:
        if not config.workflow_path.exists():
            return []
        try:
            with config.workflow_path.open("r", encoding="utf-8") as file:
                workflow = json.load(file)
        except Exception:
            return [
                config.positive_node_id,
                config.negative_node_id,
                config.seed_node_id,
                config.width_node_id,
                config.height_node_id,
                config.save_prefix_node_id,
            ]
    required = [
        config.positive_node_id,
        config.negative_node_id,
        config.seed_node_id,
        config.width_node_id,
        config.height_node_id,
        config.save_prefix_node_id,
    ]
    return [
        node_id
        for node_id in required
        if node_id not in workflow or "inputs" not in workflow[node_id]
    ]


def apply_panel_to_workflow(
    workflow: dict,
    config: ComfyUIWorkflowConfig,
    panel: Panel,
    filename_prefix: str,
) -> dict:
    patched = copy.deepcopy(workflow)
    positive_prompt = apply_text_policy(
        panel.generation.prompt or panel.prompt, panel.generation.text_policy
    )
    patched[config.positive_node_id]["inputs"]["text"] = positive_prompt
    patched[config.negative_node_id]["inputs"]["text"] = panel.generation.negative_prompt
    patched[config.seed_node_id]["inputs"]["seed"] = panel.generation.seed
    if panel.generation.width is not None:
        patched[config.width_node_id]["inputs"]["width"] = panel.generation.width
    if panel.generation.height is not None:
        patched[config.height_node_id]["inputs"]["height"] = panel.generation.height
    patched[config.save_prefix_node_id]["inputs"]["filename_prefix"] = filename_prefix
    preset = panel.generation.workflow_preset
    if preset:
        if preset.checkpoint_node_id and preset.checkpoint_name:
            patch_workflow_input(
                patched,
                preset.checkpoint_node_id,
                "ckpt_name",
                preset.checkpoint_name,
                "checkpoint",
            )
        if preset.vae_node_id and preset.vae_name:
            patch_workflow_input(patched, preset.vae_node_id, "vae_name", preset.vae_name, "VAE")
        if preset.sampler_node_id:
            sampler_values = {
                "sampler_name": preset.sampler_name or None,
                "scheduler": preset.scheduler or None,
                "steps": preset.steps,
                "cfg": preset.cfg,
                "denoise": preset.denoise,
            }
            for input_name, value in sampler_values.items():
                if value is not None:
                    patch_workflow_input(
                        patched, preset.sampler_node_id, input_name, value, "sampler"
                    )
    lora_nodes: set[str] = set()
    for lora in panel.generation.loras:
        if lora.node_id in lora_nodes:
            raise ValueError(f"複数キャラで同じLoRAノードを指定しています: {lora.node_id}")
        lora_nodes.add(lora.node_id)
        node = patched.get(lora.node_id)
        if not node or "inputs" not in node:
            raise ValueError(f"LoRAノードがworkflowにありません: {lora.node_id}")
        node["inputs"]["lora_name"] = lora.lora_name
        node["inputs"]["strength_model"] = lora.strength_model
        node["inputs"]["strength_clip"] = lora.strength_clip
    return patched


def patch_workflow_input(workflow: dict, node_id: str, input_name: str, value, label: str) -> None:
    node = workflow.get(node_id)
    if not node or "inputs" not in node:
        raise ValueError(f"{label}ノードがworkflowにありません: {node_id}")
    node["inputs"][input_name] = value


async def apply_reference_images_to_workflow(
    client: httpx.AsyncClient,
    base_url: str,
    workflow: dict,
    panel: Panel,
    export_dir: Path,
    project_id: str,
) -> None:
    reference_nodes: set[str] = set()
    for binding in panel.generation.reference_images:
        if binding.node_id in reference_nodes:
            raise ValueError(f"複数キャラで同じ参照画像ノードを指定しています: {binding.node_id}")
        reference_nodes.add(binding.node_id)
        node = workflow.get(binding.node_id)
        if not node or "inputs" not in node:
            raise ValueError(f"参照画像ノードがworkflowにありません: {binding.node_id}")
        # 参照画像は相対アセットID（project/...形式）で保存されるため、export_dir基準で解決する。
        try:
            source = resolve_asset_path(binding.asset, export_dir)
        except ValueError as exc:
            raise ValueError(f"参照画像が見つかりません: {binding.asset}") from exc
        if not source.exists():
            raise ValueError(f"参照画像が見つかりません: {binding.asset}")
        subfolder = f"local-doujin-studio/{project_id}/references"
        with source.open("rb") as image_file:
            response = await client.post(
                f"{base_url}/upload/image",
                files={"image": (source.name, image_file, "image/png")},
                data={"type": "input", "subfolder": subfolder, "overwrite": "true"},
            )
        response.raise_for_status()
        uploaded = response.json()
        uploaded_name = uploaded.get("name", source.name)
        uploaded_subfolder = uploaded.get("subfolder", subfolder)
        node["inputs"]["image"] = f"{uploaded_subfolder}/{uploaded_name}".replace("\\", "/")


def apply_text_policy(prompt: str, text_policy: str) -> str:
    if text_policy != "no_text":
        return prompt
    prompt_lower = prompt.lower()
    if NO_TEXT_PROMPT_SUFFIX in prompt_lower:
        return prompt
    if not prompt.strip():
        return NO_TEXT_PROMPT_SUFFIX
    return f"{prompt}, {NO_TEXT_PROMPT_SUFFIX}"


async def wait_for_history(
    client: httpx.AsyncClient, base_url: str, prompt_id: str, timeout_seconds: float
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"{base_url}/history/{prompt_id}")
        if response.status_code == 200:
            history = response.json()
            if prompt_id in history:
                return history
        await asyncio.sleep(1.0)
    raise TimeoutError(f"ComfyUI生成がタイムアウトしました: {prompt_id}")


async def open_comfyui_websocket(base_url: str, client_id: str) -> ClientConnection | None:
    websocket_url = base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    try:
        return await asyncio.wait_for(
            connect(f"{websocket_url}/ws?clientId={client_id}"), timeout=2.0
        )
    except Exception:
        return None


async def wait_for_websocket_completion(
    websocket: ClientConnection,
    prompt_id: str,
    timeout_seconds: float,
    progress_callback: Callable[[int, int, str | None, str], Awaitable[None]] | None,
) -> None:
    async with asyncio.timeout(timeout_seconds):
        while True:
            message = await websocket.recv()
            if not isinstance(message, str):
                continue
            event = json.loads(message)
            event_type = event.get("type")
            data = event.get("data", {})
            event_prompt_id = data.get("prompt_id")
            if event_prompt_id and event_prompt_id != prompt_id:
                continue
            if event_type == "progress":
                current = int(data.get("value", 0))
                total = max(int(data.get("max", 1)), 1)
                if progress_callback:
                    await progress_callback(current, total, data.get("node"), "ComfyUIで生成中です")
            elif event_type == "executing":
                node = data.get("node")
                if node is None and event_prompt_id == prompt_id:
                    if progress_callback:
                        await progress_callback(1, 1, None, "ComfyUI生成が完了しました")
                    return
                if progress_callback:
                    await progress_callback(0, 1, node, f"ノード {node} を実行中です")
            elif event_type in {"execution_error", "execution_interrupted"}:
                raise RuntimeError(data.get("exception_message") or "ComfyUIで生成に失敗しました")


def find_first_image(history: dict, prompt_id: str) -> dict[str, str]:
    prompt_history = history.get(prompt_id, {})
    outputs = prompt_history.get("outputs", {})
    for node_output in outputs.values():
        images = node_output.get("images", [])
        if images:
            image = images[0]
            return {
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
    raise RuntimeError("ComfyUI履歴に画像出力がありません")


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
