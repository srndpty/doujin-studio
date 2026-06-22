"""ヘルスチェック・外部サービス状態・静的アセットのHTTPルーター。"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import fonts as font_registry
from ..assets import resolve_asset_path
from ..image_backends import get_comfyui_status
from ..llm import get_llm_status
from ..schemas import ComfyUIStatusResponse, FontInfo, FontsResponse, LLMStatusResponse

router = APIRouter()


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/fonts", response_model=FontsResponse)
def list_fonts() -> FontsResponse:
    fonts = [FontInfo(**item) for item in font_registry.list_fonts()]
    path = font_registry.find_dialogue_font_path()
    primary = font_registry.dialogue_font_is_primary()
    return FontsResponse(
        dialogue_font=("源暎アンチック" if primary else (path.name if path else "PIL既定")),
        dialogue_font_available=path is not None,
        fonts=fonts,
    )


@router.get("/api/fonts/dialogue/file")
def get_dialogue_font() -> FileResponse:
    path = font_registry.find_dialogue_font_path()
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="写植用フォントが見つかりません")
    return FileResponse(path, media_type="font/ttf", filename=path.name)


@router.get("/api/comfyui/status", response_model=ComfyUIStatusResponse)
async def comfyui_status(request: Request) -> ComfyUIStatusResponse:
    status = await get_comfyui_status(request.app.state.settings)
    return ComfyUIStatusResponse(**status.__dict__)


@router.get("/api/llm/status", response_model=LLMStatusResponse)
async def llm_status(request: Request) -> LLMStatusResponse:
    status = await get_llm_status(request.app.state.settings)
    return LLMStatusResponse(**status.__dict__)


@router.get("/api/assets/{asset_id:path}")
def get_asset(asset_id: str, request: Request) -> FileResponse:
    try:
        target = resolve_asset_path(asset_id, request.app.state.settings.export_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="アセットが見つかりません") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="アセットが見つかりません")
    return FileResponse(target)
