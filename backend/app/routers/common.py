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
from fastapi.responses import JSONResponse
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
    JobEnqueueConflictError,
    JobEnqueueScopeConflictError,
    SyncGenerationError,
    find_panel_and_page,
)
from ..jobs import GenerationJob, JobManager
from ..mutation import (
    EpochMismatchError,
    InvalidProjectJsonError,
    PanelNotFoundError,
    ProjectConflictError,
    ProjectNotFoundError,
    ProjectReplaceConflictError,
    RenderCommitConflictError,
    WorkerScopeViolationError,
)
from ..project_render_service import (
    CbzPreflightError,
    CbzSelectionInconsistentError,
    RenderEpochChangedError,
    RenderSelectionInconsistentError,
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


def _error_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def register_exception_handlers(app: FastAPI) -> None:
    """domain/service例外をHTTP応答へ一括変換する。

    RouterはHTTP境界としてDTO整形や入力検証だけを担当し、CAS再試行やjob状態遷移は
    service層へ寄せる。domain例外のHTTP化はここで集約する。
    """

    @app.exception_handler(ProjectNotFoundError)
    async def _project_not_found(_request: Request, _exc: ProjectNotFoundError) -> JSONResponse:
        return _error_response(404, "プロジェクトが見つかりません")

    @app.exception_handler(PanelNotFoundError)
    async def _panel_not_found(_request: Request, _exc: PanelNotFoundError) -> JSONResponse:
        return _error_response(404, "コマが見つかりません")

    # ProjectConflictErrorのサブクラスは、操作文脈ごとに従来の409文言を維持する。
    # Starletteはtype(exc).__mro__を辿って最初に一致したhandlerを使うため、サブクラスを
    # 個別に登録すれば汎用handlerより優先される。
    @app.exception_handler(ProjectConflictError)
    async def _project_conflict(_request: Request, _exc: ProjectConflictError) -> JSONResponse:
        return _error_response(
            409,
            "他の操作（生成完了や別タブの保存）で更新されています。最新を読み込み直してください。",
        )

    @app.exception_handler(ProjectReplaceConflictError)
    async def _replace_conflict(
        _request: Request, _exc: ProjectReplaceConflictError
    ) -> JSONResponse:
        return _error_response(409, "他の操作で更新されています。最新を読み込み直してください。")

    @app.exception_handler(RenderCommitConflictError)
    async def _render_commit_conflict(
        _request: Request, _exc: RenderCommitConflictError
    ) -> JSONResponse:
        return _error_response(409, "描画結果の確定中に競合しました")

    @app.exception_handler(JobEnqueueConflictError)
    async def _job_enqueue_conflict(
        _request: Request, _exc: JobEnqueueConflictError
    ) -> JSONResponse:
        return _error_response(409, "ジョブ登録中に競合しました。再実行してください。")

    @app.exception_handler(JobEnqueueScopeConflictError)
    async def _job_enqueue_scope_conflict(
        _request: Request, _exc: JobEnqueueScopeConflictError
    ) -> JSONResponse:
        return _error_response(409, "対象コマまたは作品構成が更新されています")

    @app.exception_handler(ActiveJobConflictError)
    async def _active_job_conflict(_request: Request, _exc: ActiveJobConflictError) -> JSONResponse:
        return _error_response(409, "対象コマまたは作品構成が更新されています")

    @app.exception_handler(EpochMismatchError)
    async def _epoch_mismatch(_request: Request, _exc: EpochMismatchError) -> JSONResponse:
        return _error_response(409, "作品構成が更新されています")

    @app.exception_handler(WorkerScopeViolationError)
    async def _worker_scope_violation(
        _request: Request, _exc: WorkerScopeViolationError
    ) -> JSONResponse:
        return _error_response(500, "worker更新の対象範囲が不正です")

    @app.exception_handler(InvalidProjectJsonError)
    async def _invalid_project_json(
        _request: Request, exc: InvalidProjectJsonError
    ) -> JSONResponse:
        return _error_response(422, exc.detail)

    @app.exception_handler(RenderInputChangedError)
    async def _render_input_changed(
        _request: Request, _exc: RenderInputChangedError
    ) -> JSONResponse:
        return _error_response(
            409, "描画中にページ内容が更新されました。再度レンダリングしてください。"
        )

    @app.exception_handler(InconsistentSelectedPanelError)
    async def _inconsistent_selected_panel(
        _request: Request, exc: InconsistentSelectedPanelError
    ) -> JSONResponse:
        return _error_response(
            409, f"{exc.panel_id}: 採用画像が欠損/不整合です。再選択または再生成してください。"
        )

    @app.exception_handler(SyncGenerationError)
    async def _sync_generation_failed(_request: Request, exc: SyncGenerationError) -> JSONResponse:
        # cancelled(入力変更・構成置換)は409、その他のbackend失敗は502。
        return _error_response(409 if exc.cancelled else 502, exc.message)

    @app.exception_handler(RenderEpochChangedError)
    async def _render_epoch_changed(
        _request: Request, _exc: RenderEpochChangedError
    ) -> JSONResponse:
        return _error_response(
            409, "レンダリング中に作品構成が更新されました。最新で再実行してください。"
        )

    @app.exception_handler(RenderSelectionInconsistentError)
    async def _render_selection_inconsistent(
        _request: Request, exc: RenderSelectionInconsistentError
    ) -> JSONResponse:
        return _error_response(
            409,
            f"{exc.panel_id}: 採用画像が欠損/不整合です。再選択または再生成してから出力してください。",
        )

    @app.exception_handler(CbzSelectionInconsistentError)
    async def _cbz_selection_inconsistent(
        _request: Request, exc: CbzSelectionInconsistentError
    ) -> JSONResponse:
        return _error_response(
            422, f"{exc.panel_id}: 採用画像が欠損/不整合です。CBZ出力前に修正してください。"
        )

    @app.exception_handler(CbzPreflightError)
    async def _cbz_preflight(_request: Request, exc: CbzPreflightError) -> JSONResponse:
        return _error_response(422, exc.detail)


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
        request.app.state.mutation.mutate_local(
            project_id, lambda latest: migrate_legacy_render_state(latest, export_dir)
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
