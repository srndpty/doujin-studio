"""Router共通のHTTP層ヘルパー。

domain/service例外をHTTPExceptionへ変換する中継、ProjectRecordロード、Manga JSON parse、
レスポンス整形、画像アップロード境界などを集約する。main.pyからendpoint実装を分離するための
受け皿で、ここがHTTP境界（HTTPExceptionを送出してよい唯一の層の一つ）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from ..asset_storage import (
    ImageValidationError,
    load_validated_image,
    publish_immutable_asset,
    save_image_atomic,
)
from ..assets import normalize_manga_assets
from ..database import (
    KnowledgeChunkRecord,
    KnowledgeSourceRecord,
    ProjectRecord,
    StoryGenerationSessionRecord,
)
from ..generation_service import (
    ActiveJobConflictError,
    PanelNotFoundError,
    find_panel_and_page,
)
from ..jobs import GenerationJob, JobManager
from ..mutation import (
    EpochMismatchError,
    InvalidProjectJsonError,
    ProjectConflictError,
    ProjectMutationService,
    ProjectNotFoundError,
)
from ..rendering import (
    InconsistentSelectedPanelError,
    RenderInputChangedError,
    migrate_legacy_render_state,
)
from ..schemas import (
    ApiErrorResponse,
    GenerationJobResponse,
    KnowledgeChunkResponse,
    KnowledgeSourceResponse,
    MangaProject,
    PreflightIssue,
    PreflightResponse,
    ProjectDetail,
    ProjectSummary,
    StorySessionSummary,
)

# 同期生成API(generate-image / render)の追加エラー契約をOpenAPIへ明示する。
# 実行時にキャンセルは409、生成バックエンド失敗は502を返す。
GENERATION_ERROR_RESPONSES: dict[int | str, dict] = {
    409: {
        "model": ApiErrorResponse,
        "description": "生成がキャンセルされた（入力変更・構成置換など）",
    },
    502: {"model": ApiErrorResponse, "description": "画像生成バックエンドが失敗した"},
}


def ensure_generation_succeeded(job: GenerationJob) -> None:
    """同期完了を要求するendpointでは失敗・キャンセルを成功レスポンスにしない。"""
    if job.status == "done":
        return
    if job.status == "cancelled":
        raise HTTPException(status_code=409, detail=job.message)
    raise HTTPException(status_code=502, detail=job.message)


def get_job_or_404(request: Request, job_id: str) -> GenerationJob:
    manager: JobManager = request.app.state.job_manager
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="生成ジョブが見つかりません")
    return job


def to_job_response(job: GenerationJob) -> GenerationJobResponse:
    return GenerationJobResponse(**job.as_dict())


def _to_preflight_response(
    project_id: str, page: int | None, issues: list[PreflightIssue]
) -> PreflightResponse:
    errors = [issue for issue in issues if issue.level == "error"]
    warnings = [issue for issue in issues if issue.level == "warning"]
    return PreflightResponse(
        project_id=project_id,
        page=page,
        ok=not errors,
        errors=errors,
        warnings=warnings,
    )


def load_project_record(request: Request, project_id: str) -> ProjectRecord:
    with request.app.state.SessionLocal() as session:
        record = request.app.state.repository.get(session, project_id)
        if record is None:
            raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
        session.expunge(record)
    manga = parse_manga_json(record.manga_json)
    export_dir = request.app.state.settings.export_dir
    if migrate_legacy_render_state(manga, export_dir):
        # 旧形式のdoneを安全側へ戻す。CAS競合時は最新へ同じ移行を再適用する。
        run_mutation(
            request.app.state.mutation,
            project_id,
            lambda latest: migrate_legacy_render_state(latest, export_dir),
        )
        with request.app.state.SessionLocal() as session:
            record = request.app.state.repository.get(session, project_id)
            if record is None:
                raise HTTPException(status_code=404, detail="プロジェクトが見つかりません")
            session.expunge(record)
        manga = parse_manga_json(record.manga_json)
    record.manga_json = normalize_manga_assets(
        manga, request.app.state.settings.export_dir
    ).model_dump_json()
    return record


def referenced_project_asset_paths(app: FastAPI, project_id: str) -> set[Path]:
    return app.state.rendering.referenced_project_asset_paths(project_id)


def cleanup_published_assets(app: FastAPI, project_id: str, ownership: dict[Path, bool]) -> None:
    app.state.rendering.cleanup_published_assets(project_id, ownership)


def commit_rendered_pages(
    app: FastAPI,
    project_id: str,
    snapshot: MangaProject,
    assets: list[Path],
    *,
    expected_revision: int | None = None,
    expected_epoch: int | None = None,
) -> tuple[MangaProject, int]:
    """RenderingServiceでdoneをCAS確定し、確定不能なdomain例外を409へ変換する。"""
    try:
        return app.state.rendering.commit_rendered_pages(
            project_id,
            snapshot,
            assets,
            expected_revision=expected_revision,
            expected_epoch=expected_epoch,
        )
    except RenderInputChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail="描画中にページ内容が更新されました。再度レンダリングしてください。",
        ) from exc
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail="描画結果の確定中に競合しました") from exc
    except EpochMismatchError as exc:
        raise HTTPException(status_code=409, detail="作品構成が更新されています") from exc


def render_and_commit_page(
    app: FastAPI,
    project_id: str,
    snapshot: MangaProject,
    snapshot_revision: int,
    page_number: int,
) -> tuple[Path, list[str], MangaProject, int]:
    """RenderingServiceで対象ページを描画・確定し、domain例外を409へ変換する。"""
    try:
        return app.state.rendering.render_and_commit_page(
            project_id, snapshot, snapshot_revision, page_number
        )
    except InconsistentSelectedPanelError as exc:
        raise HTTPException(
            status_code=409,
            detail=(f"{exc.panel_id}: 採用画像が欠損/不整合です。再選択または再生成してください。"),
        ) from exc
    except RenderInputChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail="描画中にページ内容が更新されました。再度レンダリングしてください。",
        ) from exc
    except ProjectConflictError as exc:
        raise HTTPException(status_code=409, detail="描画結果の確定中に競合しました") from exc
    except EpochMismatchError as exc:
        raise HTTPException(status_code=409, detail="作品構成が更新されています") from exc


async def save_request_image(request: Request, target: Path, preserve_alpha: bool = False) -> None:
    """Requestを読み取り、検証済みPNGを原子的に保存する（HTTP境界）。"""
    content = await request.body()
    try:
        image = load_validated_image(content, preserve_alpha=preserve_alpha)
    except ImageValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    save_image_atomic(image, target)


async def save_content_addressed_request_image(
    request: Request,
    asset_dir: Path,
    asset_kind: str,
    *,
    preserve_alpha: bool = False,
) -> tuple[Path, bool]:
    """正規化済みPNGの内容hashを持つ不変assetとして保存する。

    返り値の2要素目`created`は、このリクエストでcanonical targetを新規公開したか。
    既存hashと一致した（他リクエスト/過去の同一内容が既にある）場合はFalseを返し、
    競合失敗時のcleanupで他リクエストのassetを誤って消さないようにする。
    """
    temporary = asset_dir / f".{asset_kind}-{uuid.uuid4().hex}.png"
    await save_request_image(request, temporary, preserve_alpha=preserve_alpha)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    target = asset_dir / f"{asset_kind}-{digest}.png"
    # exists()事前確認だと同一内容の並行公開で両者がcreated=Trueになり、
    # 失敗側cleanupが成功側assetを消しうる。os.linkで原子的に「初公開か」を判定する。
    created = publish_immutable_asset(temporary, target)
    return target, created


def parse_manga_json(raw: str) -> MangaProject:
    try:
        return MangaProject.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"Manga JSONが不正です: {exc}") from exc


def find_panel(manga: MangaProject, panel_id: str):
    panel = find_panel_optional(manga, panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail="コマが見つかりません")
    return panel


def find_panel_optional(manga: MangaProject, panel_id: str):
    panel, _page = find_panel_and_page(manga, panel_id)
    return panel


def run_mutation(service: ProjectMutationService, project_id: str, mutate, expected_revision=None):
    """ProjectMutationServiceを呼び、サービス例外をHTTPExceptionへ変換する。"""
    try:
        return service.mutate(project_id, mutate, expected_revision=expected_revision)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "他の操作（生成完了や別タブの保存）で更新されています。"
                "最新を読み込み直してください。"
            ),
        ) from exc
    except InvalidProjectJsonError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def replace_project(
    service: ProjectMutationService,
    project_id: str,
    manga: MangaProject,
    *,
    expected_revision: int,
    increment_epoch: bool = False,
) -> tuple[MangaProject, int]:
    try:
        return service.replace(
            project_id,
            manga,
            expected_revision=expected_revision,
            increment_epoch=increment_epoch,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="他の操作で更新されています。最新を読み込み直してください。",
        ) from exc
    except InvalidProjectJsonError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def replace_project_with_history(
    service: ProjectMutationService,
    project_id: str,
    build_replacement,
    *,
    expected_revision: int,
    history_label: str,
) -> tuple[MangaProject, int]:
    try:
        return service.replace_with_history(
            project_id,
            build_replacement,
            expected_revision=expected_revision,
            history_label=history_label,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="他の操作で更新されています。最新を読み込み直してください。",
        ) from exc
    except InvalidProjectJsonError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc


def enqueue_panel_jobs(
    app: FastAPI,
    project_id: str,
    panel_ids: list[str],
    candidate_count: int,
    message: str,
    *,
    skip_active: bool = False,
    expected_epoch: int | None = None,
) -> list[GenerationJob]:
    """panelのqueued化・GenerationJobRecord追加・revision更新を単一トランザクションで確定する。

    別トランザクションに分けると、間でプロセス停止した際に「panelはqueuedだが対応ジョブが無い」
    という復旧不能な状態が残るため、同じSQLiteトランザクションで原子的に行う。
    commit後にメモリ登録し、Taskは呼び出し側がcommit確認後に開始する。
    """
    try:
        jobs = app.state.generation.enqueue(
            project_id,
            panel_ids,
            candidate_count,
            message,
            skip_active=skip_active,
            expected_epoch=expected_epoch,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません") from exc
    except PanelNotFoundError as exc:
        raise HTTPException(status_code=404, detail="コマが見つかりません") from exc
    except (ActiveJobConflictError, EpochMismatchError) as exc:
        raise HTTPException(
            status_code=409, detail="対象コマまたは作品構成が更新されています"
        ) from exc
    except ProjectConflictError as exc:
        raise HTTPException(
            status_code=409, detail="ジョブ登録中に競合しました。再実行してください。"
        ) from exc
    manager: JobManager = app.state.job_manager
    for job in jobs:
        manager.register_in_memory(job)
    return jobs


def find_panel_page_number(manga: MangaProject, panel_id: str) -> int:
    for page in manga.pages:
        for panel in page.panels:
            if panel.panel_id == panel_id:
                return page.page
    raise HTTPException(status_code=404, detail="コマが見つかりません")


def to_knowledge_source(record: KnowledgeSourceRecord) -> KnowledgeSourceResponse:
    return KnowledgeSourceResponse.model_validate(
        {
            "id": record.id,
            "work_name": record.work_name,
            "title": record.title,
            "doc_type": record.doc_type,
            "usage": record.usage,
            "chunk_count": record.chunk_count,
            "created_at": record.created_at,
        }
    )


def to_knowledge_chunk(record: KnowledgeChunkRecord) -> KnowledgeChunkResponse:
    return KnowledgeChunkResponse.model_validate(
        {
            "id": record.id,
            "source_id": record.source_id,
            "work_name": record.work_name,
            "usage": record.usage,
            "kind": record.kind,
            "title": record.title,
            "content": record.content,
            "policy": record.policy,
            "tags": [tag for tag in record.tags.split(", ") if tag],
            "position": record.position,
        }
    )


def to_story_summary(record: StoryGenerationSessionRecord) -> StorySessionSummary:
    return StorySessionSummary(
        id=record.id,
        project_id=record.project_id,
        work_name=record.work_name,
        target_pages=record.target_pages,
        instruction=record.instruction,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_summary(record: ProjectRecord) -> ProjectSummary:
    return ProjectSummary(
        id=record.id,
        title=record.title,
        work_name=record.work_name,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_detail(record: ProjectRecord, export_dir: Path | None = None) -> ProjectDetail:
    manga = parse_manga_json(record.manga_json)
    if export_dir is not None:
        normalize_manga_assets(manga, export_dir)
    return ProjectDetail(**to_summary(record).model_dump(), manga_json=manga)


def open_in_file_manager(path: Path) -> None:
    target = path.resolve()
    if sys.platform == "win32":
        command = (
            ["explorer.exe", f"/select,{target}"]
            if target.is_file()
            else ["explorer.exe", str(target)]
        )
    elif sys.platform == "darwin":
        command = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
    else:
        command = ["xdg-open", str(target.parent if target.is_file() else target)]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
