"""Modal 1.6+ App definition: Ubuntu 24.04 Image, aria2c worker, and FastAPI ASGI gateway."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# Add the src directory for a local file-based deploy. In the container this
# file also exists at /root/modal_app.py, alongside the already importable utd package.
_APP_DIR = Path(__file__).resolve().parent
if _APP_DIR.name == "cloud" and _APP_DIR.parent.name == "utd":
    _SRC_DIR = _APP_DIR.parent.parent
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))

import modal  # noqa: E402

from utd.cloud.aria2_engine import Aria2JobSupervisor  # noqa: E402
from utd.cloud.config import (  # noqa: E402
    APP_NAME,
    AUTH_SECRET_NAME,
    CONTROL_QUEUE_NAME,
    DOWNLOADS_ROOT_REL,
    GATEWAY_SCALEDOWN_SECONDS,
    JOBS_DICT_NAME,
    LOGS_QUEUE_NAME,
    MAX_STORED_LOGS_PER_JOB,
    VOLUME_COMMIT_INTERVAL_SECONDS,
    VOLUME_MOUNT_PATH,
    VOLUME_NAME,
    WORKER_TIMEOUT_SECONDS,
)
from utd.cloud.server import create_cloud_app  # noqa: E402
from utd.cloud.volume_store import ModalVolumeStore, VolumeStore  # noqa: E402
from utd.shared.bencode import inspect_torrent_bytes  # noqa: E402
from utd.shared.models import (  # noqa: E402
    TERMINAL_STATUSES,
    HealthInfo,
    JobCommand,
    JobInfo,
    JobOptionsPatch,
    JobStatus,
    LogEvent,
    StreamEvent,
    TorrentOptions,
    parse_magnet_info,
    sanitize_volume_path,
)
from utd.shared.version import get_package_version  # noqa: E402


def _resolve_secrets() -> list[modal.Secret]:
    """Attach auth secret when deploying or when UTD_API_TOKEN is supplied locally."""
    inline_token = os.environ.get("UTD_API_TOKEN", "").strip()
    if inline_token:
        return [modal.Secret.from_dict({"UTD_API_TOKEN": inline_token})]

    if not AUTH_SECRET_NAME:
        return []

    # Avoid network calls during offline unit test imports; hydrate only under Modal CLI/deploy.
    is_modal_cli = "modal" in Path(sys.argv[0]).name.lower() or os.environ.get("UTD_DEPLOY") == "1"
    if modal.is_local() and not is_modal_cli:
        return []

    secret = modal.Secret.from_name(AUTH_SECRET_NAME)
    if modal.is_local():
        try:
            secret.hydrate()
        except Exception:
            return []
    return [secret]


def _ignore_non_cloud_files(path: Path) -> bool:
    """Exclude non-Python assets and client-only files from the Modal cloud image."""
    if path.name == "__pycache__" or "client" in path.parts:
        return True
    if path.is_dir():
        return False
    return path.suffix != ".py"


ubuntu_image = (
    modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
    .entrypoint([])
    .env(
        {
            "DEBIAN_FRONTEND": "noninteractive",
            "PYTHONUNBUFFERED": "1",
        }
    )
    .apt_install(
        "aria2",
        "ca-certificates",
        "tzdata",
    )
    .uv_pip_install(
        "fastapi[standard]>=0.115.0",
        "httpx>=0.28.0",
        "pydantic>=2.10.0",
        "python-multipart>=0.0.20",
        "uvicorn>=0.34.0",
        "websockets>=14.0",
    )
    .add_local_python_source("utd", ignore=_ignore_non_cloud_files)
    # File-based `modal deploy` imports this entrypoint as `modal_app`, not
    # `utd.cloud.modal_app`. include_source=False disables Modal's implicit
    # entrypoint mount, so explicitly provide that top-level module as well.
    .add_local_file(Path(__file__), remote_path="/root/modal_app.py")
)

app = modal.App(name=APP_NAME, image=ubuntu_image, include_source=False)

downloads_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
jobs_dict = modal.Dict.from_name(JOBS_DICT_NAME, create_if_missing=True)
logs_queue = modal.Queue.from_name(LOGS_QUEUE_NAME, create_if_missing=True)
control_queue = modal.Queue.from_name(CONTROL_QUEUE_NAME, create_if_missing=True)

_WORKER_SECRETS = _resolve_secrets()
_REQUIRE_PROXY_AUTH = os.environ.get("UTD_REQUIRE_PROXY_AUTH", "0") == "1"


def _job_partition(job_id: str) -> str:
    return f"job-{job_id}"


@app.function(
    image=ubuntu_image,
    volumes={VOLUME_MOUNT_PATH: downloads_volume},
    timeout=WORKER_TIMEOUT_SECONDS,
    cpu=2.0,
    memory=2048,
)
async def run_torrent_job(
    job_payload: dict[str, Any],
    *,
    source_uri: str | None = None,
    torrent_bytes: bytes | None = None,
    trackers: list[str] | None = None,
    is_private: bool = False,
) -> dict[str, Any]:
    """Modal Function executing headless aria2c and persisting output to the Modal Volume."""
    job = JobInfo.model_validate(job_payload)
    out_dir = Path(VOLUME_MOUNT_PATH) / sanitize_volume_path(job.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Persist payload in Dict under a dedicated private key for future resume if container stops.
    await jobs_dict.put.aio(
        f"__payload__:{job.job_id}",
        {
            "source_uri": source_uri,
            "torrent_bytes": torrent_bytes,
            "trackers": trackers,
            "is_private": is_private,
        },
    )

    last_progress_push = 0.0
    last_dict_write = 0.0
    queue_events_sent = 0

    async def on_progress(updated: JobInfo) -> None:
        nonlocal last_progress_push, last_dict_write, queue_events_sent
        now = time.monotonic()
        is_terminal = updated.status in TERMINAL_STATUSES
        if not is_terminal and (now - last_dict_write) < 0.7:
            return
        last_dict_write = now

        if len(updated.logs) > MAX_STORED_LOGS_PER_JOB:
            updated.logs = updated.logs[-MAX_STORED_LOGS_PER_JOB:]
        await jobs_dict.put.aio(updated.job_id, updated.model_dump())

        if is_terminal or (queue_events_sent < 3500 and (now - last_progress_push) >= 1.0):
            last_progress_push = now
            queue_events_sent += 1
            evt_type = "status" if is_terminal else "progress"
            evt = StreamEvent(
                type=evt_type,
                job_id=updated.job_id,
                ts=updated.updated_at,
                data=updated.model_dump(exclude={"logs"}),
            ).model_dump()
            with contextlib.suppress(Exception):
                await logs_queue.put.aio(evt, partition=_job_partition(updated.job_id))
            with contextlib.suppress(Exception):
                await logs_queue.put.aio(evt, partition="__all__", partition_ttl=3600)

    async def on_log(log_event: LogEvent) -> None:
        nonlocal queue_events_sent
        if queue_events_sent >= 4500:
            return
        queue_events_sent += 1
        evt = StreamEvent(
            type="log",
            job_id=job.job_id,
            seq=log_event.seq,
            ts=log_event.ts,
            data=log_event.model_dump(),
        ).model_dump()
        with contextlib.suppress(Exception):
            await logs_queue.put.aio(evt, partition=_job_partition(job.job_id))

    async def on_commit() -> None:
        await downloads_volume.commit.aio()

    supervisor = Aria2JobSupervisor(
        job=job,
        output_dir=out_dir,
        source_uri=source_uri,
        torrent_bytes=torrent_bytes,
        trackers=trackers,
        is_private=is_private,
        on_progress=on_progress,
        on_log=on_log,
        on_commit=on_commit,
        commit_interval=VOLUME_COMMIT_INTERVAL_SECONDS,
    )

    stop_ctrl = asyncio.Event()

    async def _poll_controls() -> None:
        while not stop_ctrl.is_set():
            try:
                raw_cmds = await control_queue.get_many.aio(
                    10, block=True, timeout=2.0, partition=job.job_id
                )
            except Exception:
                raw_cmds = []
            for raw in raw_cmds:
                with contextlib.suppress(Exception):
                    cmd = JobCommand.model_validate(raw)
                    await supervisor.enqueue_command(cmd)

    ctrl_task = asyncio.create_task(_poll_controls())
    try:
        final_job = await supervisor.run()
    finally:
        stop_ctrl.set()
        ctrl_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ctrl_task
        with contextlib.suppress(Exception):
            await downloads_volume.commit.aio()

    return final_job.model_dump()


class ModalTorrentBackend:
    """`CloudBackend` implementation backed by Modal Volume, Dict, Queue, and Function spawns."""

    def __init__(self) -> None:
        self._volume_store = ModalVolumeStore(
            downloads_volume,
            mount_root=Path(VOLUME_MOUNT_PATH),
        )

    @property
    def volume(self) -> VolumeStore:
        return self._volume_store

    async def health(self) -> HealthInfo:
        jobs = await self.list_jobs()
        active = sum(1 for j in jobs if j.status not in TERMINAL_STATUSES)
        return HealthInfo(
            version=get_package_version(),
            engine="aria2c",
            volume_name=VOLUME_NAME,
            active_jobs=active,
        )

    @staticmethod
    def _output_rel(options: TorrentOptions) -> str:
        return (
            f"{DOWNLOADS_ROOT_REL}/{options.destination_subdir}"
            if options.destination_subdir
            else DOWNLOADS_ROOT_REL
        )

    async def _spawn_worker(
        self,
        job: JobInfo,
        *,
        source_uri: str | None,
        torrent_bytes: bytes | None,
        trackers: list[str] | None,
        is_private: bool,
    ) -> JobInfo:
        await jobs_dict.put.aio(job.job_id, job.model_dump())
        call = await run_torrent_job.spawn.aio(
            job.model_dump(),
            source_uri=source_uri,
            torrent_bytes=torrent_bytes,
            trackers=trackers,
            is_private=is_private,
        )
        job.modal_call_id = call.object_id
        job.updated_at = time.time()
        await jobs_dict.put.aio(job.job_id, job.model_dump())

        evt = StreamEvent(
            type="status",
            job_id=job.job_id,
            ts=job.updated_at,
            data=job.model_dump(exclude={"logs"}),
        ).model_dump()
        with contextlib.suppress(Exception):
            await logs_queue.put.aio(evt, partition="__all__", partition_ttl=3600)
        return job

    async def submit_magnet(self, uri: str, options: TorrentOptions) -> JobInfo:
        lower = uri.strip().lower()
        if lower.startswith("magnet:?"):
            info = parse_magnet_info(uri)
            name = info["name"]
            info_hash = info["info_hash"]
            trackers = info["trackers"]
            source_type = "magnet"
        else:
            name = uri.rstrip("/").split("/")[-1] or "download"
            info_hash = None
            trackers = []
            source_type = "url"

        job = JobInfo(
            job_id=uuid.uuid4().hex[:12],
            name=name,
            source_type=source_type,
            info_hash=info_hash,
            status=JobStatus.QUEUED,
            options=options,
            output_dir=self._output_rel(options),
        )
        return await self._spawn_worker(
            job,
            source_uri=uri.strip(),
            torrent_bytes=None,
            trackers=trackers,
            is_private=False,
        )

    async def submit_torrent(self, data: bytes, filename: str, options: TorrentOptions) -> JobInfo:
        meta = inspect_torrent_bytes(data)
        job = JobInfo(
            job_id=uuid.uuid4().hex[:12],
            name=meta.name or filename,
            source_type="torrent",
            info_hash=meta.info_hash,
            status=JobStatus.QUEUED,
            options=options,
            output_dir=self._output_rel(options),
            total_bytes=meta.total_size,
            files=meta.files,
        )
        return await self._spawn_worker(
            job,
            source_uri=None,
            torrent_bytes=data,
            trackers=meta.trackers,
            is_private=meta.is_private,
        )

    async def _reconcile_job(self, job: JobInfo) -> JobInfo:
        if job.status in TERMINAL_STATUSES or not job.modal_call_id:
            return job
        try:
            call = modal.FunctionCall.from_id(job.modal_call_id)
            try:
                await call.get.aio(timeout=0)
            except TimeoutError:
                return job
            except Exception as exc:
                job.status = JobStatus.ERROR
                job.error = f"Worker terminated unexpectedly: {exc}"
                job.download_speed = 0
                job.upload_speed = 0
                job.finished_at = time.time()
                job.updated_at = job.finished_at
                await jobs_dict.put.aio(job.job_id, job.model_dump())
        except Exception:
            pass
        return job

    async def list_jobs(self) -> list[JobInfo]:
        jobs: list[JobInfo] = []
        async for key, val in jobs_dict.items.aio():
            if not isinstance(key, str) or key.startswith("__payload__:"):
                continue
            with contextlib.suppress(Exception):
                job = JobInfo.model_validate(val)
                jobs.append(await self._reconcile_job(job))
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    async def get_job(self, job_id: str) -> JobInfo | None:
        raw = await jobs_dict.get.aio(job_id)
        if raw is None:
            return None
        job = JobInfo.model_validate(raw)
        return await self._reconcile_job(job)

    async def send_command(self, job_id: str, command: JobCommand) -> JobInfo:
        job = await self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job '{job_id}' not found.")

        # If resuming a stopped/errored/cancelled job, respawn worker with `--continue=true`.
        if command.action == "resume" and job.status in (JobStatus.ERROR, JobStatus.CANCELLED):
            payload = await jobs_dict.get.aio(f"__payload__:{job_id}")
            if payload:
                job.status = JobStatus.QUEUED
                job.error = None
                job.finished_at = None
                return await self._spawn_worker(
                    job,
                    source_uri=payload.get("source_uri"),
                    torrent_bytes=payload.get("torrent_bytes"),
                    trackers=payload.get("trackers"),
                    is_private=bool(payload.get("is_private", False)),
                )

        if command.action == "update_options" and command.options is not None:
            patch: JobOptionsPatch = command.options
            if patch.max_download_limit is not None:
                job.options.max_download_limit = patch.max_download_limit
            if patch.max_upload_limit is not None:
                job.options.max_upload_limit = patch.max_upload_limit
            if patch.max_peers is not None:
                job.options.max_peers = patch.max_peers
            job.updated_at = time.time()
            await jobs_dict.put.aio(job.job_id, job.model_dump())

        await control_queue.put.aio(command.model_dump(), partition=job_id)
        return job

    async def delete_job(self, job_id: str, *, delete_files: bool = False) -> bool:
        job = await self.get_job(job_id)
        if job is None:
            return False
        if job.status not in TERMINAL_STATUSES:
            with contextlib.suppress(Exception):
                await control_queue.put.aio(
                    JobCommand(action="cancel", delete_files=delete_files).model_dump(),
                    partition=job_id,
                )
            if job.modal_call_id:
                with contextlib.suppress(Exception):
                    await modal.FunctionCall.from_id(job.modal_call_id).cancel.aio()
        elif delete_files:
            base = sanitize_volume_path(job.output_dir)
            for f in job.files:
                rel = sanitize_volume_path(f.path)
                if not rel:
                    continue
                target = f"{base}/{rel}" if base else rel
                with contextlib.suppress(Exception):
                    await self._volume_store.remove(target, recursive=True)

        with contextlib.suppress(Exception):
            await jobs_dict.pop.aio(job_id)
        with contextlib.suppress(Exception):
            await jobs_dict.pop.aio(f"__payload__:{job_id}")
        with contextlib.suppress(Exception):
            await logs_queue.clear.aio(partition=_job_partition(job_id))
        return True

    async def subscribe(
        self, job_id: str | None = None, *, since_seq: int = 0
    ) -> AsyncIterator[StreamEvent]:
        out_q: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

        if job_id is not None:
            job = await self.get_job(job_id)
            if job is not None:
                yield StreamEvent(
                    type="snapshot",
                    job_id=job.job_id,
                    ts=time.time(),
                    data=job.model_dump(exclude={"logs"}),
                )
                for log_item in job.logs:
                    if log_item.seq > since_seq:
                        yield StreamEvent(
                            type="log",
                            job_id=job.job_id,
                            seq=log_item.seq,
                            ts=log_item.ts,
                            data=log_item.model_dump(),
                        )
        else:
            for job in await self.list_jobs():
                yield StreamEvent(
                    type="snapshot",
                    job_id=job.job_id,
                    ts=time.time(),
                    data=job.model_dump(exclude={"logs"}),
                )

        partition = _job_partition(job_id) if job_id is not None else "__all__"
        stop_evt = asyncio.Event()

        async def _tail_queue() -> None:
            try:
                async for raw in logs_queue.iterate.aio(
                    partition=partition, item_poll_timeout=110.0
                ):
                    if stop_evt.is_set():
                        break
                    with contextlib.suppress(Exception):
                        evt = StreamEvent.model_validate(raw)
                        if evt.type == "log" and evt.seq <= since_seq:
                            continue
                        await out_q.put(evt)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                await out_q.put(None)

        async def _reconcile_loop() -> None:
            last_updated: dict[str, float] = {}
            while not stop_evt.is_set():
                await asyncio.sleep(1.5)
                if job_id is not None:
                    cur = await self.get_job(job_id)
                    if cur and last_updated.get(cur.job_id) != cur.updated_at:
                        last_updated[cur.job_id] = cur.updated_at
                        await out_q.put(
                            StreamEvent(
                                type="progress"
                                if cur.status not in TERMINAL_STATUSES
                                else "status",
                                job_id=cur.job_id,
                                ts=cur.updated_at,
                                data=cur.model_dump(exclude={"logs"}),
                            )
                        )
                else:
                    for cur in await self.list_jobs():
                        if last_updated.get(cur.job_id) != cur.updated_at:
                            last_updated[cur.job_id] = cur.updated_at
                            await out_q.put(
                                StreamEvent(
                                    type="progress"
                                    if cur.status not in TERMINAL_STATUSES
                                    else "status",
                                    job_id=cur.job_id,
                                    ts=cur.updated_at,
                                    data=cur.model_dump(exclude={"logs"}),
                                )
                            )

        tail_task = asyncio.create_task(_tail_queue())
        reconcile_task = asyncio.create_task(_reconcile_loop())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(out_q.get(), timeout=20.0)
                    if item is None:
                        break
                    yield item
                except TimeoutError:
                    yield StreamEvent(
                        type="ping",
                        job_id=job_id or "*",
                        ts=time.time(),
                        data={},
                    )
        finally:
            stop_evt.set()
            tail_task.cancel()
            reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tail_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reconcile_task


@app.cls(
    image=ubuntu_image,
    volumes={VOLUME_MOUNT_PATH: downloads_volume},
    scaledown_window=GATEWAY_SCALEDOWN_SECONDS,
    cpu=1.0,
    memory=1024,
    secrets=_WORKER_SECRETS,
)
@modal.concurrent(max_inputs=100)
class WebGateway:
    """Modal ASGI gateway serving FastAPI REST, WebSockets, SSE, and Volume File Explorer."""

    @modal.asgi_app(requires_proxy_auth=_REQUIRE_PROXY_AUTH)
    def web(self):
        return create_cloud_app(ModalTorrentBackend())
