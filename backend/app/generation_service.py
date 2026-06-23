"""画像生成ジョブ登録のトランザクション境界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from asyncio import CancelledError
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .assets import normalize_manga_assets, resolve_asset_path
from .config import Settings
from .database import GenerationJobRecord, now_utc
from .image_backends import build_image_backend
from .jobs import TERMINAL_JOB_STATUSES, GenerationJob, JobManager
from .mutation import (
    EpochMismatchError,
    PanelNotFoundError,
    ProjectConflictError,
    ProjectMutationService,
    ProjectNotFoundError,
    ProjectRevisionConflictError,
    mark_page_dirty,
    parse_manga,
)
from .prompt_composer import prepare_panel_for_generation
from .repository import ProjectRepository
from .schemas import ImageCandidate, MangaProject, Page, Panel

if TYPE_CHECKING:
    from .rendering import RenderingService


@dataclass
class GenerationRuntime:
    settings: Settings
    jobs: JobManager
    mutation: ProjectMutationService
    rendering: "RenderingService"
    repository: ProjectRepository


class ActiveJobConflictError(ProjectConflictError):
    """登録対象panelが既に同epochで生成中。"""


class JobEnqueueScopeConflictError(ProjectConflictError):
    """ジョブ登録時の世代/対象コマ競合（epoch不一致・active重複）。"""


class JobEnqueueConflictError(ProjectConflictError):
    """ジョブ登録CASがリトライ上限まで競合した。"""


class GenerationInputMismatchError(Exception):
    pass


class JobOwnershipLostError(Exception):
    pass


class SyncGenerationError(Exception):
    """同期生成endpointでジョブがdone以外で終端した。

    cancelled(入力変更・構成置換など)は409、その他のbackend失敗は502へ変換する。
    """

    def __init__(self, message: str, *, cancelled: bool) -> None:
        super().__init__(message)
        self.message = message
        self.cancelled = cancelled


def ensure_sync_generation_succeeded(job: GenerationJob) -> None:
    """同期完了を要求する経路で、失敗・キャンセルを成功扱いにしない。"""
    if job.status == "done":
        return
    raise SyncGenerationError(job.message, cancelled=job.status == "cancelled")


# リモート(ComfyUI)停止結果の区分。UI表示にも使う。
RemoteCancelState = Literal["not_requested", "queued_removed", "interrupted", "failed"]


def find_panel_and_page(manga: MangaProject, panel_id: str):
    for page in manga.pages:
        for panel in page.panels:
            if panel.panel_id == panel_id:
                return panel, page
    return None, None


def generation_input_hash(manga: MangaProject, panel, export_dir: Path) -> str:
    """実際にbackendへ渡す生成入力と参照画像内容から安定hashを作る。"""
    generated = prepare_panel_for_generation(manga, panel)
    payload = generated.model_dump(
        exclude={
            "image_asset",
            "image_candidates",
            "selected_candidate_id",
            "dialogue",
            "sfx",
        }
    )
    generation = payload["generation"]
    # status等の揮発フィールドとbackendを除外する。backendは実際の生成先ではなく
    # （実際はsettingsのbuild_image_backendで決まる）採用候補の記録/表示用なので入力ではない。
    # 一方seedは実際にbackendへ渡す生成入力なので除外しない。これによりユーザーのseed編集は
    # 検出しつつ、候補採用ではseedを書き換えない（apply_candidate_selection参照）ため、
    # 採用し直しだけで入力変更と誤判定することはない。
    for volatile in ("status", "message", "prompt_id", "backend"):
        generation.pop(volatile, None)
    asset_ids = {reference.asset for reference in generated.control_references} | {
        reference.asset for reference in generated.generation.reference_images
    }
    asset_digests: dict[str, str] = {}
    for asset_id in sorted(asset_ids):
        try:
            path = resolve_asset_path(asset_id, export_dir)
            asset_digests[asset_id] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            )
        except (OSError, ValueError):
            asset_digests[asset_id] = "missing"
    payload["asset_digests"] = asset_digests
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def apply_candidate_selection(panel, candidate: ImageCandidate) -> None:
    panel.selected_candidate_id = candidate.id
    panel.image_asset = candidate.asset
    panel.generation.backend = candidate.backend
    panel.generation.status = candidate.status
    # generation.seedは「次の生成の基準seed」というユーザー入力なので、候補採用では書き換えない。
    # 採用候補のseedはcandidate.seedに記録済みで表示もそこから行う。ここで上書きすると
    # 候補選び直しが入力変更扱いになり、進行中ジョブの候補が破棄されてしまう。
    panel.generation.prompt_id = candidate.prompt_id
    panel.generation.message = candidate.message


def apply_generation_result(panel, result) -> None:
    panel.image_asset = str(result.asset_path) if result.asset_path else None
    panel.generation.backend = result.backend
    panel.generation.status = result.status
    panel.generation.message = result.message
    panel.generation.prompt_id = result.prompt_id


def register_generation_candidate(panel, generated_panel, result, candidate_id=None) -> None:
    if result.asset_path is None:
        apply_generation_result(panel, result)
        return
    candidate = ImageCandidate(
        id=candidate_id or str(uuid.uuid4()),
        asset=str(result.asset_path),
        backend=result.backend,
        status=result.status,
        prompt=generated_panel.generation.prompt or generated_panel.prompt,
        negative_prompt=generated_panel.generation.negative_prompt,
        characters=list(generated_panel.characters),
        loras=list(generated_panel.generation.loras),
        reference_images=list(generated_panel.generation.reference_images),
        workflow_preset=generated_panel.generation.workflow_preset,
        seed=generated_panel.generation.seed,
        prompt_id=result.prompt_id,
        message=result.message,
        created_at=now_utc(),
    )
    panel.image_candidates.append(candidate)
    apply_candidate_selection(panel, candidate)


def find_active_panel_job(
    manager: JobManager, project_id: str, panel_id: str
) -> GenerationJob | None:
    return next(
        (
            item
            for item in manager.jobs.values()
            if item.project_id == project_id
            and item.panel_id == panel_id
            and item.status not in TERMINAL_JOB_STATUSES
        ),
        None,
    )


def stop_comfyui_generation(settings: Settings, prompt_id: str | None) -> RemoteCancelState:
    """ComfyUI側の対象生成だけを停止する。

    /interruptはprompt_idを指定できない「現在実行中の処理を止める」グローバル操作なので、
    対象promptが実行中だと確認できたときだけ使う。キュー待ちならprompt_id指定でqueueから
    削除する。対象が見当たらなければ無関係な生成を巻き込まないよう何もしない。
    """
    if settings.image_backend.lower() != "comfyui" or not prompt_id:
        return "not_requested"
    base = settings.comfyui_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{base}/queue")
            response.raise_for_status()
            queue = response.json()
            running = {entry[1] for entry in queue.get("queue_running", []) if len(entry) > 1}
            pending = {entry[1] for entry in queue.get("queue_pending", []) if len(entry) > 1}
            if prompt_id in pending:
                deleted = client.post(f"{base}/queue", json={"delete": [prompt_id]})
                deleted.raise_for_status()
                return "queued_removed"
            if prompt_id in running:
                interrupted = client.post(f"{base}/interrupt")
                interrupted.raise_for_status()
                return "interrupted"
            # 対象がキューに無い（完了済み/別物）。グローバルinterruptは避ける。
            return "not_requested"
    except Exception:
        return "failed"


def job_may_stop_panel(generation, job_id: str) -> bool:
    """停止/失敗更新を、panel所有権を持つjobだけに限定する。

    所有者(active_job_id)が自分なら更新可。後方互換として、所有者未設定(None)の
    旧形式jobはまだqueued/runningのときだけ解放を許す。これにより、後続の新jobが完了して
    active_job_id=Noneへ戻した後に、遅れて到着した旧jobの停止処理がdoneをskippedへ
    戻すのを防ぐ。
    """
    if generation.active_job_id == job_id:
        return True
    return generation.active_job_id is None and generation.status in {"queued", "running"}


class GenerationService:
    def __init__(
        self,
        session_factory: sessionmaker,
        export_dir: Path,
        runtime: GenerationRuntime | None = None,
        repository: ProjectRepository | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.export_dir = export_dir
        self.runtime = runtime
        self.repository = repository or (
            runtime.repository if runtime is not None else ProjectRepository()
        )

    def require_runtime(self) -> GenerationRuntime:
        if self.runtime is None:
            raise RuntimeError("GenerationRuntimeが設定されていません")
        return self.runtime

    def enqueue(
        self,
        project_id: str,
        panel_ids: list[str],
        candidate_count: int,
        message: str,
        *,
        attempts: int = 5,
        skip_active: bool = False,
        expected_epoch: int | None = None,
        expected_revision: int | None = None,
    ) -> list[GenerationJob]:
        """panelのqueued化・job追加・revision更新を同一CASトランザクションで確定する。

        expected_epoch指定時は、呼び出し元が固定した世代と一致する場合のみ登録する。
        長時間の/renderが構成全置換をまたいで新作品へジョブを積むのを防ぐ。
        """
        # expected_epoch指定なら最初から固定。未指定なら初回読み取りで固定する。
        required_epoch: int | None = expected_epoch
        for _ in range(attempts):
            with self.session_factory() as session:
                record = self.repository.get(session, project_id)
                if record is None:
                    raise ProjectNotFoundError()
                base_revision = record.revision
                if expected_revision is not None and base_revision != expected_revision:
                    raise ProjectRevisionConflictError(project_id, expected_revision)
                if required_epoch is None:
                    required_epoch = record.generation_epoch
                elif record.generation_epoch != required_epoch:
                    raise JobEnqueueScopeConflictError()
                manga = parse_manga(record.manga_json)
                panels = {panel.panel_id: panel for page in manga.pages for panel in page.panels}
                if any(panel_id not in panels for panel_id in panel_ids):
                    raise PanelNotFoundError()
                # 旧epochのactive履歴は現在世代の登録を妨げない。候補保存はepoch CASで
                # 拒否されるため、DB上もcancelledへ終端化して部分一意indexを解放する。
                self.repository.cancel_active_jobs_other_epoch(
                    session, project_id, panel_ids, required_epoch
                )
                active_db = self.repository.active_panel_ids(
                    session, project_id, panel_ids, required_epoch
                )
                # JSON表示だけactiveで同epochのDB jobが無ければ孤立状態として自己修復する。
                for panel_id in panel_ids:
                    panel = panels[panel_id]
                    if (
                        panel.generation.status in {"queued", "running"}
                        and panel_id not in active_db
                    ):
                        panel.generation.status = "pending"
                        panel.generation.prompt_id = None
                        panel.generation.active_job_id = None
                        panel.generation.message = "対応する生成ジョブがないため状態を復旧しました"
                active_panels = active_db
                if active_panels and not skip_active:
                    raise ActiveJobConflictError()
                panel_ids = [panel_id for panel_id in panel_ids if panel_id not in active_panels]
                if not panel_ids:
                    raise ActiveJobConflictError()
                jobs: list[GenerationJob] = []
                for panel_id in panel_ids:
                    panel = panels[panel_id]
                    job = GenerationJob(
                        project_id=project_id,
                        panel_id=panel_id,
                        candidate_count=candidate_count,
                        epoch=record.generation_epoch,
                        status="queued",
                        message=message,
                    )
                    panel.generation.status = "queued"
                    panel.generation.active_job_id = job.id
                    panel.generation.message = message
                    jobs.append(job)
                normalize_manga_assets(manga, self.export_dir)
                if (
                    self.repository.cas_set_manga(
                        session,
                        project_id,
                        base_revision,
                        manga,
                        require_epoch=required_epoch,
                    )
                    != 1
                ):
                    session.rollback()
                    if expected_revision is not None:
                        raise ProjectRevisionConflictError(project_id, expected_revision)
                    continue
                for job in jobs:
                    self.repository.add_generation_job(
                        session,
                        GenerationJobRecord(
                            id=job.id,
                            project_id=project_id,
                            panel_id=job.panel_id,
                            candidate_count=job.candidate_count,
                            epoch=job.epoch,
                            status=job.status,
                            message=job.message,
                            candidate_ids_json="[]",
                            created_at=job.created_at,
                            updated_at=job.updated_at,
                        ),
                    )
                try:
                    session.commit()
                    return jobs
                except IntegrityError as exc:
                    session.rollback()
                    raise ActiveJobConflictError() from exc
        raise JobEnqueueConflictError()

    def start(
        self,
        project_id: str,
        panel_ids: list[str],
        candidate_count: int,
        message: str,
        *,
        skip_active: bool = False,
        expected_epoch: int | None = None,
        expected_revision: int | None = None,
    ) -> list[GenerationJob]:
        """ジョブ登録後、メモリ登録とTask起動までをGenerationServiceへ集約する。"""
        runtime = self.require_runtime()
        jobs = self.enqueue(
            project_id,
            panel_ids,
            candidate_count,
            message,
            skip_active=skip_active,
            expected_epoch=expected_epoch,
            expected_revision=expected_revision,
        )
        for job in jobs:
            runtime.jobs.register_in_memory(job)
            runtime.jobs.start(job, self.run(job))
        return jobs

    def find_active_panel_job(self, project_id: str, panel_id: str) -> GenerationJob | None:
        runtime = self.require_runtime()
        return find_active_panel_job(runtime.jobs, project_id, panel_id)

    def update_panel_in_latest(
        self, project_id: str, panel_id: str, mutate, *, expected_epoch: int
    ) -> MangaProject:
        """最新のmanga_jsonへ対象パネル限定のworker mutationを適用してCAS保存する。

        mutateは ``(panel, page)`` を受け取る薄い用途向け。mangaの読み取りが必要な
        候補保存などは mutate_worker_panel を直接使う。
        """
        result = self.require_runtime().mutation.mutate_worker_panel(
            project_id,
            panel_id=panel_id,
            expected_epoch=expected_epoch,
            mutate=lambda _manga, panel, page: mutate(panel, page),
        )
        return result.project.manga

    def mark_panel_job_stopped(self, job: GenerationJob, message: str, error: bool = False) -> None:
        desired_status = "error" if error else "skipped"
        runtime = self.require_runtime()
        # API側とTask側の両方から呼ばれるため、同一状態ならCAS更新自体を省略する。
        try:
            with self.session_factory() as session:
                record = runtime.repository.get(session, job.project_id)
                if record is None or record.generation_epoch != job.epoch:
                    return
                current, _page = find_panel_and_page(parse_manga(record.manga_json), job.panel_id)
                if current is None or not job_may_stop_panel(current.generation, job.id):
                    return
                if (
                    current.generation.status == desired_status
                    and current.generation.message == message
                    and current.generation.active_job_id is None
                ):
                    return
        except Exception:
            return

        def mutate(panel, page) -> None:
            if not job_may_stop_panel(panel.generation, job.id):
                return
            panel.generation.status = desired_status
            panel.generation.prompt_id = None
            panel.generation.active_job_id = None
            panel.generation.message = message

        try:
            # 構造全置換後に古いTaskのキャンセル処理が到着しても、新しい同名panelの
            # generation状態を書き換えない。
            self.update_panel_in_latest(
                job.project_id, job.panel_id, mutate, expected_epoch=job.epoch
            )
        except Exception:
            return

    async def await_completion(self, job: GenerationJob) -> GenerationJob:
        """既に開始済みのジョブの完了を待つ（ComfyUI呼び出しはワーカー側に一本化）。"""
        manager = self.require_runtime().jobs
        task = manager.tasks.get(job.id)
        if task is not None:
            # run()は例外を内部で状態に反映するため、ここでは伝播させない。
            await asyncio.gather(task, return_exceptions=True)
        return manager.get(job.id) or job

    async def cancel(self, job: GenerationJob) -> GenerationJob:
        """ローカルTask・panel状態・ComfyUI prompt停止を一箇所で行う。"""
        runtime = self.require_runtime()
        manager = runtime.jobs
        prompt_id = job.prompt_id
        if not manager.cancel(job):
            return job
        self.mark_panel_job_stopped(job, "生成をキャンセルしました")
        remote = await asyncio.to_thread(stop_comfyui_generation, runtime.settings, prompt_id)
        if remote == "failed":
            manager.update(
                job,
                status="cancelled",
                message="ローカルではキャンセルしましたが、ComfyUI側の停止に失敗しました",
            )
        task = manager.tasks.get(job.id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return job

    async def cancel_before_epoch(self, project_id: str, new_epoch: int) -> None:
        """構造置換成功後、旧epochのローカルTaskとComfyUI promptだけを停止する。"""
        runtime = self.require_runtime()
        manager = runtime.jobs
        cancelled_tasks: list[asyncio.Task] = []
        remote_cancels = []
        cancelled_jobs: list[GenerationJob] = []
        for item in list(manager.jobs.values()):
            if not (
                item.project_id == project_id
                and item.epoch < new_epoch
                and item.status not in TERMINAL_JOB_STATUSES
            ):
                continue
            prompt_id = item.prompt_id
            if not manager.cancel(item):
                continue
            self.mark_panel_job_stopped(item, "生成をキャンセルしました")
            cancelled_jobs.append(item)
            remote_cancels.append(
                asyncio.to_thread(stop_comfyui_generation, runtime.settings, prompt_id)
            )
            task = manager.tasks.get(item.id)
            if task is not None:
                cancelled_tasks.append(task)
        if remote_cancels:
            results = await asyncio.gather(*remote_cancels, return_exceptions=True)
            for job, result in zip(cancelled_jobs, results, strict=True):
                if result == "failed" or isinstance(result, Exception):
                    manager.update(
                        job,
                        message="作品構成変更により停止しましたが、ComfyUI側の停止に失敗しました",
                    )
        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)

    async def run(self, job: GenerationJob) -> None:
        manager = self.require_runtime().jobs
        manager.update(job, message="生成キューで待機中です")
        try:
            async with manager.generation_lock:
                await self.execute(job)
        except CancelledError:
            # API側で即時解放できなかった経路（shutdown等）と競合時の冪等な保険。
            # job状態はHTTPキャンセルのmanager.cancel()またはshutdown()が既に確定している。
            if manager.shutting_down:
                # shutdown()はrunningジョブだけをerrorへ確定し、未開始のqueuedジョブは
                # 次回起動のrestore_pendingで再開するため状態を変えずに残す。queuedジョブの
                # panelをerrorにすると、DBは再開対象なのにManga JSONだけ停止エラーになり食い違う。
                if job.status != "queued":
                    self.mark_panel_job_stopped(
                        job,
                        "バックエンド停止により中断されました。必要なら再実行してください",
                        error=True,
                    )
            else:
                self.mark_panel_job_stopped(job, "生成をキャンセルしました")
            raise

    async def execute(self, job: GenerationJob) -> None:
        runtime = self.require_runtime()
        manager: JobManager = runtime.jobs
        # このジョブが生成した候補PNG。epoch/入力不一致での破棄時に未参照分を回収する。
        # 最初のepochチェックで例外が出てもexceptで参照できるよう、try外で初期化する。
        candidate_assets: dict[Path, bool] = {}
        try:

            def set_running(panel, page) -> None:
                # 復旧した旧形式jobは未設定を許してここで所有権を取得する。新jobがすでに
                # 所有している場合、遅延した旧Taskはpanelを更新してはならない。
                if panel.generation.active_job_id not in {None, job.id}:
                    raise JobOwnershipLostError()
                panel.generation.active_job_id = job.id
                panel.generation.status = "running"
                panel.generation.message = "画像候補を生成中です"

            # 最新を読み直して対象パネルだけ更新し、並行編集を踏みつぶさない。
            # 構成全置換後（世代不一致）なら、ここで破棄して候補を新作品へ混ぜない。
            manga = self.update_panel_in_latest(
                job.project_id, job.panel_id, set_running, expected_epoch=job.epoch
            )
            panel, _page = find_panel_and_page(manga, job.panel_id)
            if panel is None:
                raise RuntimeError("コマが見つかりません")
            prepared_panel = prepare_panel_for_generation(manga, panel)
            input_hash = generation_input_hash(manga, panel, runtime.settings.export_dir)
            manager.update(
                job,
                status="running",
                generation_input_hash=input_hash,
                message="画像候補を生成中です",
            )
            backend = build_image_backend(runtime.settings)
            # 開始時の選択状態を記録し、生成中にユーザーが選び直したら自動選択で上書きしない。
            selection_state: dict[str, str | bool | None] = {
                "base": panel.selected_candidate_id,
                "auto": panel.selected_candidate_id,
                "allow": True,
            }

            for candidate_index in range(job.candidate_count):
                candidate_id = str(uuid.uuid4())
                generated_panel = prepared_panel.model_copy(deep=True)
                generated_panel.generation.seed += candidate_index
                target = (
                    runtime.settings.export_dir
                    / job.project_id
                    / "panels"
                    / job.panel_id
                    / f"{candidate_id}.png"
                )
                # 生成前に所有権を記録する。CancelledError/キャンセル/不一致のいずれでも、
                # 採用されなかったPNGを未参照判定で確実に回収できるようにする。
                candidate_assets[target.resolve()] = True

                async def report_progress(
                    current: int,
                    total: int,
                    node: str | None,
                    message: str,
                    candidate_number: int = candidate_index,
                ) -> None:
                    fraction = current / max(total, 1)
                    overall = round(((candidate_number + fraction) / job.candidate_count) * 100)
                    manager.update(
                        job,
                        progress=max(0, min(overall, 99)),
                        current=current,
                        total=total,
                        node=node,
                        message=f"候補 {candidate_number + 1}/{job.candidate_count}: {message}",
                    )

                async def store_prompt_id(prompt_id: str) -> None:
                    manager.update(job, prompt_id=prompt_id)

                result = await backend.generate_panel(
                    job.project_id,
                    generated_panel,
                    runtime.settings.export_dir,
                    target_path=target,
                    progress_callback=report_progress,
                    on_prompt_id=store_prompt_id,
                )
                # backendが別パスへ書いた場合も所有権へ含める（通常はtargetと同一）。
                if result.asset_path is not None:
                    candidate_assets[Path(result.asset_path).resolve()] = True
                # キャンセル要求後に生成物が返っても、対象ジョブがキャンセル済みなら保存しない。
                # PNGを回収し、panel状態もskippedへ確定する（runningのまま固定されると、
                # 次回enqueueでactive扱いになり再生成不能になるため）。
                if job.status == "cancelled":
                    runtime.rendering.cleanup_published_assets(job.project_id, candidate_assets)
                    self.mark_panel_job_stopped(job, "生成をキャンセルしました")
                    return
                if result.asset_path is None:
                    raise RuntimeError("生成画像の保存先が返りませんでした")
                candidate = ImageCandidate(
                    id=candidate_id,
                    asset=str(result.asset_path),
                    # backendはImageResult上はstr。Pydanticが実値を検証するためcastで型を渡す。
                    backend=cast(Literal["stub", "comfyui"], result.backend),
                    status=cast(Literal["done", "fallback", "error"], result.status),
                    prompt=generated_panel.generation.prompt or generated_panel.prompt,
                    negative_prompt=generated_panel.generation.negative_prompt,
                    characters=list(generated_panel.characters),
                    loras=list(generated_panel.generation.loras),
                    reference_images=list(generated_panel.generation.reference_images),
                    workflow_preset=generated_panel.generation.workflow_preset,
                    seed=generated_panel.generation.seed,
                    prompt_id=result.prompt_id,
                    message=result.message,
                    created_at=now_utc(),
                )

                def add_candidate_to_latest(
                    latest: MangaProject,
                    target_panel: Panel,
                    target_page: Page,
                    new_candidate: ImageCandidate = candidate,
                    is_last_candidate: bool = candidate_index == job.candidate_count - 1,
                ) -> bool:
                    if target_panel.generation.active_job_id != job.id:
                        raise JobOwnershipLostError()
                    latest_hash = generation_input_hash(
                        latest, target_panel, runtime.settings.export_dir
                    )
                    if latest_hash != job.generation_input_hash:
                        owned_candidate_ids = set(job.candidate_ids)
                        target_panel.image_candidates = [
                            item
                            for item in target_panel.image_candidates
                            if item.id not in owned_candidate_ids
                        ]
                        if target_panel.selected_candidate_id in owned_candidate_ids:
                            target_panel.selected_candidate_id = None
                            target_panel.image_asset = None
                        if owned_candidate_ids and target_page is not None:
                            mark_page_dirty(target_page)
                        target_panel.generation.status = "pending"
                        target_panel.generation.prompt_id = None
                        target_panel.generation.active_job_id = None
                        target_panel.generation.message = (
                            "生成中に入力が変更されたため、古い候補を破棄しました"
                        )
                        return False
                    target_panel.image_candidates.append(new_candidate)
                    current = target_panel.selected_candidate_id
                    # 開始時の選択、またはジョブ自身が直前に自動選択した状態から変わっていなければ
                    # 新候補を自動選択する。生成中にユーザーが別候補を選んでいたら追加のみに留める。
                    if selection_state["allow"] and current in (
                        selection_state["base"],
                        selection_state["auto"],
                        None,
                    ):
                        apply_candidate_selection(target_panel, new_candidate)
                        selection_state["auto"] = new_candidate.id
                        # 採用画像が変わったので、対象ページを再レンダリング対象へ戻す。
                        if target_page is not None:
                            mark_page_dirty(target_page)
                    else:
                        selection_state["allow"] = False
                    if is_last_candidate:
                        target_panel.generation.active_job_id = None
                    return True

                # 候補ごとに最新を読み直して対象panelだけへマージし、生成中のユーザー編集を
                # 残す。panel限定APIに通すことで、候補保存が誤って他panel・page構造・project
                # 全体を書き換えないことをCAS確定前に保証する。構成全置換後（世代不一致）なら
                # 候補を保存せず破棄する（EpochMismatchError）。
                mutation_result = runtime.mutation.mutate_worker_panel(
                    job.project_id,
                    panel_id=job.panel_id,
                    expected_epoch=job.epoch,
                    mutate=add_candidate_to_latest,
                )
                stored = mutation_result.result
                if not stored:
                    raise GenerationInputMismatchError()
                job.candidate_ids.append(candidate_id)
                manager.update(
                    job,
                    progress=round(((candidate_index + 1) / job.candidate_count) * 100),
                    # 次候補投入前にprompt_idをクリアし、古いidでのリモート停止誤爆を防ぐ。
                    prompt_id=None,
                    message=f"候補 {candidate_index + 1}/{job.candidate_count} を保存しました",
                )

            manager.update(
                job,
                status="done",
                progress=100,
                node=None,
                prompt_id=None,
                message="画像候補の生成が完了しました",
            )
        except CancelledError:
            # API/Task双方からのpanel更新はmark_panel_job_stopped()の冪等性へ委ねる。
            # 採用されなかった生成PNGはここで回収する（current/history参照分は残る）。
            runtime.rendering.cleanup_published_assets(job.project_id, candidate_assets)
            raise
        except EpochMismatchError:
            # ネーム再生成・ストーリー適用・復元で作品構成が置き換わった。古いプロンプトの
            # 候補を新作品へ混ぜないよう、保存せずジョブをキャンセル扱いにする。
            runtime.rendering.cleanup_published_assets(job.project_id, candidate_assets)
            manager.update(
                job,
                status="cancelled",
                node=None,
                prompt_id=None,
                message="作品構成が変わったため生成を破棄しました",
            )
        except GenerationInputMismatchError:
            # 破棄した候補のPNG本体も、current/history未参照なら回収する。
            runtime.rendering.cleanup_published_assets(job.project_id, candidate_assets)
            manager.update(
                job,
                status="cancelled",
                node=None,
                prompt_id=None,
                message="生成入力が変わったため古い候補を破棄しました",
            )
        except JobOwnershipLostError:
            # 同epochの後続jobがpanel所有権を取得済み。旧jobは後続状態へ触れず終端化する。
            runtime.rendering.cleanup_published_assets(job.project_id, candidate_assets)
            manager.update(
                job,
                status="cancelled",
                node=None,
                prompt_id=None,
                message="後続の生成が開始されたため古い生成を破棄しました",
            )
        except Exception as exc:
            # backendがPNGを書いた後に例外化した場合の未参照PNGを回収する。
            runtime.rendering.cleanup_published_assets(job.project_id, candidate_assets)
            if job.status == "cancelled":
                self.mark_panel_job_stopped(job, "生成をキャンセルしました")
                return
            self.mark_panel_job_stopped(job, f"画像候補の生成に失敗しました: {exc}", error=True)
            manager.update(
                job,
                status="error",
                node=None,
                prompt_id=None,
                message=f"画像候補の生成に失敗しました: {exc}",
            )
