"""Cloud backend abstraction and local in-process implementation for testing/self-hosting."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol

from utd.cloud.aria2_engine import Aria2JobSupervisor
from utd.cloud.config import DOWNLOADS_ROOT_REL, VOLUME_NAME
from utd.cloud.volume_store import LocalVolumeStore, VolumeStore
from utd.shared.bencode import inspect_torrent_bytes
from utd.shared.models import (
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
from utd.shared.version import get_package_version


class CloudBackend(Protocol):
    """Contract satisfied by both `ModalTorrentBackend` and `LocalTorrentBackend`."""

    @property
    def volume(self) -> VolumeStore: ...

    async def health(self) -> HealthInfo: ...

    async def submit_magnet(self, uri: str, options: TorrentOptions) -> JobInfo: ...

    async def submit_torrent(
        self, data: bytes, filename: str, options: TorrentOptions
    ) -> JobInfo: ...

    async def list_jobs(self) -> list[JobInfo]: ...

    async def get_job(self, job_id: str) -> JobInfo | None: ...

    async def send_command(self, job_id: str, command: JobCommand) -> JobInfo: ...

    async def delete_job(self, job_id: str, *, delete_files: bool = False) -> bool: ...

    def subscribe(
        self, job_id: str | None = None, *, since_seq: int = 0
    ) -> AsyncIterator[StreamEvent]: ...


WorkerFactory = Callable[
    [
        JobInfo,
        Path,
        str | None,
        bytes | None,
        list[str] | None,
        bool,
        Callable[[JobInfo], Awaitable[None]],
        Callable[[LogEvent], Awaitable[None]],
    ],
    Awaitable[None],
]


class LocalTorrentBackend:
    """In-process / local disk backend implementing `CloudBackend` for tests & standalone runs."""

    def __init__(
        self,
        root_dir: Path,
        *,
        use_aria2: bool = True,
        worker_runner: WorkerFactory | None = None,
    ) -> None:
        self.root_dir = root_dir.resolve()
        self.volume_store = LocalVolumeStore(self.root_dir)
        self.use_aria2 = use_aria2
        self.worker_runner = worker_runner
        self._jobs: dict[str, JobInfo] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._supervisors: dict[str, Aria2JobSupervisor] = {}
        self._subscribers: set[tuple[str | None, asyncio.Queue[StreamEvent]]] = set()
        self._source_payloads: dict[
            str, tuple[str | None, bytes | None, list[str] | None, bool]
        ] = {}

    @property
    def volume(self) -> VolumeStore:
        return self.volume_store

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _broadcast(self, event: StreamEvent) -> None:
        for target_job_id, queue in list(self._subscribers):
            if target_job_id is None or target_job_id == event.job_id:
                await queue.put(event)

    async def health(self) -> HealthInfo:
        active = sum(1 for j in self._jobs.values() if j.status not in TERMINAL_STATUSES)
        return HealthInfo(
            version=get_package_version(),
            engine="aria2c" if self.use_aria2 else "custom",
            volume_name=VOLUME_NAME,
            active_jobs=active,
        )

    def _job_output_dir(self, options: TorrentOptions) -> tuple[str, Path]:
        rel = (
            f"{DOWNLOADS_ROOT_REL}/{options.destination_subdir}"
            if options.destination_subdir
            else DOWNLOADS_ROOT_REL
        )
        out = (self.root_dir / rel).resolve()
        out.mkdir(parents=True, exist_ok=True)
        return rel, out

    async def _launch_job(
        self,
        job: JobInfo,
        *,
        source_uri: str | None,
        torrent_bytes: bytes | None,
        trackers: list[str] | None,
        is_private: bool,
    ) -> None:
        self._jobs[job.job_id] = job
        self._source_payloads[job.job_id] = (source_uri, torrent_bytes, trackers, is_private)
        _, out_dir = self._job_output_dir(job.options)

        async def on_progress(updated: JobInfo) -> None:
            self._jobs[updated.job_id] = updated.model_copy(deep=True)
            await self._broadcast(
                StreamEvent(
                    type="progress" if updated.status not in TERMINAL_STATUSES else "status",
                    job_id=updated.job_id,
                    ts=updated.updated_at,
                    data=updated.model_dump(exclude={"logs"}),
                )
            )

        async def on_log(log_event: LogEvent) -> None:
            await self._broadcast(
                StreamEvent(
                    type="log",
                    job_id=job.job_id,
                    seq=log_event.seq,
                    ts=log_event.ts,
                    data=log_event.model_dump(),
                )
            )

        if self.worker_runner is not None:
            runner = self.worker_runner

            async def _run_custom() -> None:
                await runner(
                    job,
                    out_dir,
                    source_uri,
                    torrent_bytes,
                    trackers,
                    is_private,
                    on_progress,
                    on_log,
                )

            self._tasks[job.job_id] = asyncio.create_task(_run_custom())
        else:
            supervisor = Aria2JobSupervisor(
                job=job,
                output_dir=out_dir,
                source_uri=source_uri,
                torrent_bytes=torrent_bytes,
                trackers=trackers,
                is_private=is_private,
                on_progress=on_progress,
                on_log=on_log,
            )
            self._supervisors[job.job_id] = supervisor
            self._tasks[job.job_id] = asyncio.create_task(
                self._run_supervisor(job.job_id, supervisor)
            )

        await on_progress(job)

    async def _run_supervisor(self, job_id: str, supervisor: Aria2JobSupervisor) -> None:
        try:
            await supervisor.run()
        finally:
            self._supervisors.pop(job_id, None)

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

        rel, _ = self._job_output_dir(options)
        job = JobInfo(
            job_id=uuid.uuid4().hex[:12],
            name=name,
            source_type=source_type,
            info_hash=info_hash,
            status=JobStatus.QUEUED,
            options=options,
            output_dir=rel,
        )
        await self._launch_job(
            job,
            source_uri=uri.strip(),
            torrent_bytes=None,
            trackers=trackers,
            is_private=False,
        )
        return self._jobs[job.job_id]

    async def submit_torrent(self, data: bytes, filename: str, options: TorrentOptions) -> JobInfo:
        meta = inspect_torrent_bytes(data)
        rel, _ = self._job_output_dir(options)
        job = JobInfo(
            job_id=uuid.uuid4().hex[:12],
            name=meta.name or filename,
            source_type="torrent",
            info_hash=meta.info_hash,
            status=JobStatus.QUEUED,
            options=options,
            output_dir=rel,
            total_bytes=meta.total_size,
            files=meta.files,
        )
        await self._launch_job(
            job,
            source_uri=None,
            torrent_bytes=data,
            trackers=meta.trackers,
            is_private=meta.is_private,
        )
        return self._jobs[job.job_id]

    async def list_jobs(self) -> list[JobInfo]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    async def get_job(self, job_id: str) -> JobInfo | None:
        return self._jobs.get(job_id)

    async def send_command(self, job_id: str, command: JobCommand) -> JobInfo:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job '{job_id}' not found.")

        supervisor = self._supervisors.get(job_id)
        if supervisor is not None:
            await supervisor.enqueue_command(command)
            return self._jobs[job_id]

        # Handle commands when running with a custom test runner or stopped job
        if command.action == "pause" and job.status not in TERMINAL_STATUSES:
            job.status = JobStatus.PAUSED
            job.download_speed = 0
            job.upload_speed = 0
        elif command.action == "resume" and job.status in (
            JobStatus.PAUSED,
            JobStatus.ERROR,
            JobStatus.CANCELLED,
        ):
            payload = self._source_payloads.get(job_id)
            if payload and job.status != JobStatus.PAUSED:
                job.status = JobStatus.QUEUED
                job.error = None
                await self._launch_job(
                    job,
                    source_uri=payload[0],
                    torrent_bytes=payload[1],
                    trackers=payload[2],
                    is_private=payload[3],
                )
            else:
                job.status = JobStatus.DOWNLOADING
        elif command.action == "stop_seeding" and job.status == JobStatus.SEEDING:
            job.status = JobStatus.COMPLETED
            job.progress = 100.0
            job.download_speed = 0
            job.upload_speed = 0
            job.finished_at = time.time()
        elif command.action == "cancel":
            task = self._tasks.get(job_id)
            if task and not task.done():
                task.cancel()
            job.status = JobStatus.CANCELLED
            job.download_speed = 0
            job.upload_speed = 0
            job.finished_at = time.time()
            if command.delete_files:
                self._delete_job_files_sync(job)
        elif command.action == "update_options" and command.options is not None:
            patch: JobOptionsPatch = command.options
            if patch.max_download_limit is not None:
                job.options.max_download_limit = patch.max_download_limit
            if patch.max_upload_limit is not None:
                job.options.max_upload_limit = patch.max_upload_limit
            if patch.max_peers is not None:
                job.options.max_peers = patch.max_peers

        job.updated_at = time.time()
        await self._broadcast(
            StreamEvent(
                type="status",
                job_id=job.job_id,
                ts=job.updated_at,
                data=job.model_dump(exclude={"logs"}),
            )
        )
        return job

    def _delete_job_files_sync(self, job: JobInfo) -> None:
        base = self.root_dir / sanitize_volume_path(job.output_dir)
        for f in job.files:
            rel = sanitize_volume_path(f.path)
            if not rel:
                continue
            target = (base / rel).resolve()
            if base in target.parents or target == base:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.is_file():
                    target.unlink(missing_ok=True)
                Path(f"{target}.aria2").unlink(missing_ok=True)

    async def delete_job(self, job_id: str, *, delete_files: bool = False) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status not in TERMINAL_STATUSES:
            await self.send_command(job_id, JobCommand(action="cancel", delete_files=delete_files))
        elif delete_files:
            self._delete_job_files_sync(job)
        self._jobs.pop(job_id, None)
        self._source_payloads.pop(job_id, None)
        return True

    async def subscribe(
        self, job_id: str | None = None, *, since_seq: int = 0
    ) -> AsyncIterator[StreamEvent]:
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        key = (job_id, queue)
        self._subscribers.add(key)
        try:
            # Emit initial snapshot(s) + replay missed recent logs
            if job_id is not None:
                job = self._jobs.get(job_id)
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

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield event
                except TimeoutError:
                    yield StreamEvent(
                        type="ping",
                        job_id=job_id or "*",
                        ts=time.time(),
                        data={},
                    )
        finally:
            self._subscribers.discard(key)
