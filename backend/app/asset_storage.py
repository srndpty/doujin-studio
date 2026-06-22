"""アセット保存の基盤（画像検証・原子的保存・内容hash不変公開・参照asset走査）。

HTTP/Requestには依存せず、画像検証は ImageValidationError を送出する。Requestの読み取りと
HTTPエラー変換は呼び出し側(boundary)が担う。docs/refactoring-plan.md の asset storage 抽出。
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from pathlib import Path

from PIL import Image

from .schemas import MangaProject

# 1コマ画像として現実的な上限。展開後のピクセル数で「圧縮爆弾」を弾く。
MAX_IMAGE_PIXELS = 64_000_000  # 約8000x8000
MAX_IMAGE_DIMENSION = 12_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Pillow自体の圧縮爆弾検知も明示的に有効化する（巨大画像でDecompressionBombErrorを投げる）。
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ImageValidationError(Exception):
    """アップロード画像が検証を通らない。呼び出し側で422へ変換する。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def load_validated_image(content: bytes, *, preserve_alpha: bool = False) -> Image.Image:
    """バイト列を検証し、正規化したPIL画像を返す。不正なら ImageValidationError。"""
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ImageValidationError("参照画像は20MB以下にしてください")
    try:
        with Image.open(io.BytesIO(content)) as source:
            # 圧縮後が小さくても展開後に巨大化しうるため、ピクセル数・縦横を先に検査する。
            width, height = source.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise ImageValidationError(
                    "画像サイズが大きすぎます（最大8000x8000・約64メガピクセル）"
                )
            # 透過オーバーフレーム（人物切り抜き等）はアルファを保持する。
            return source.convert("RGBA" if preserve_alpha else "RGB")
    except ImageValidationError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageValidationError("画像サイズが大きすぎます") from exc
    except Exception as exc:
        raise ImageValidationError("参照画像を読み込めません") from exc


def save_image_atomic(image: Image.Image, target: Path) -> None:
    """検証済み画像を一時ファイルへ書き出し、成功後にreplaceで原子的に差し替える。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    # 同じ参照先への並行アップロードで一時ファイルが衝突しないよう、リクエストごとに一意化する。
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        image.save(temporary, format="PNG")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def publish_immutable_asset(staged: Path, target: Path) -> bool:
    """既存成果物を上書きせず、同内容なら再利用する。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged, target)
        created = True
    except FileExistsError as exc:
        if (
            hashlib.sha256(staged.read_bytes()).digest()
            != hashlib.sha256(target.read_bytes()).digest()
        ):
            raise RuntimeError(f"不変アセットの内容が一致しません: {target.name}") from exc
        created = False
    finally:
        staged.unlink(missing_ok=True)
    return created


def iter_manga_asset_strings(manga: MangaProject):
    """Manga JSON内の「アセットを指すフィールド」だけを列挙する。

    prompt・台詞・作品名など任意文字列をパス候補にすると、長文で
    OSError(File name too long)を誘発し、cleanupを巻き込んでジョブを停止不能にする。
    そのためassetフィールドを明示的に収集する。
    """
    for character in manga.characters:
        if character.reference_image_asset:
            yield character.reference_image_asset
    for location in manga.locations:
        if location.reference_image_asset:
            yield location.reference_image_asset
    for page in manga.pages:
        if page.render_asset:
            yield page.render_asset
        for overlay in page.overlay_elements:
            if overlay.asset:
                yield overlay.asset
            if overlay.mask_asset:
                yield overlay.mask_asset
        for panel in page.panels:
            if panel.image_asset:
                yield panel.image_asset
            for control in panel.control_references:
                if control.asset:
                    yield control.asset
            for reference in panel.generation.reference_images:
                if reference.asset:
                    yield reference.asset
            for candidate in panel.image_candidates:
                if candidate.asset:
                    yield candidate.asset
                for reference in candidate.reference_images:
                    if reference.asset:
                        yield reference.asset
