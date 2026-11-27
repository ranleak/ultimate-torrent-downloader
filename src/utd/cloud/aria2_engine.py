"""High-performance async aria2c JSON-RPC 2.0 supervisor."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import secrets
import shutil
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from utd.shared.models import (
    JobCommand,
    JobInfo,
    JobOptionsPatch,
    JobStatus,
    LogEvent,
    TorrentFileSpec,
    TorrentOptions,
)
from utd.shared.trackers import merged_trackers_csv

logger = logging.getLogger(__name__)


def find_free_tcp_port() -> int:
    """Bind an ephemeral TCP port on localhost and return its port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _can_listen_on_ipv6() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        # Match aria2's loopback RPC listener. A kernel may support ::1 but have
        # no configured IPv6 interface, in which case AI_ADDRCONFIG rejects it.
        addresses = socket.getaddrinfo(
            None, 0, socket.AF_INET6, socket.SOCK_STREAM, flags=socket.AI_ADDRCONFIG
        )
        if not addresses:
            return False
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(addresses[0][4])
    except OSError:
        return False
    return True


def build_aria2c_args(
    *,
    output_dir: Path,
    rpc_port: int,
    rpc_secret: str,
    options: TorrentOptions,
    trackers: list[str] | None = None,
    is_private: bool = False,
    enable_ipv6: bool = False,
) -> list[str]:
    """Construct speed-optimized headless aria2c CLI flags for Modal Volume storage."""
    args = [
        "aria2c",
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={rpc_port}",
        f"--rpc-secret={rpc_secret}",
        f"--dir={output_dir}",
        # Avoid posix_fallocate stalls/errors on virtualized & FUSE Modal Volumes.
        "--file-allocation=none",
        f"--disk-cache={options.disk_cache_mb}M",
        "--continue=true",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        f"--max-connection-per-server={options.max_connection_per_server}",
        f"--split={options.split}",
        "--min-split-size=1M",
        f"--max-overall-download-limit={options.max_download_limit}",
        f"--max-overall-upload-limit={options.max_upload_limit}",
        f"--bt-max-peers={options.max_peers}",
        "--bt-request-peer-speed-limit=20M",
        "--enable-dht=true",
        "--enable-dht6=true" if enable_ipv6 else "--enable-dht6=false",
        "--dht-listen-port=6881-6999",
        "--listen-port=6881-6999",
        "--enable-peer-exchange=true",
        "--bt-enable-lpd=true",
        "--bt-save-metadata=true",
        "--bt-load-saved-metadata=true",
        "--bt-metadata-only=false",
        "--follow-torrent=true",
        "--bt-remove-unselected-file=true",
        f"--seed-time={options.seed_time_minutes}",
        f"--seed-ratio={options.seed_ratio}",
        "--summary-interval=10",
        "--console-log-level=notice",
        "--enable-color=false",
        "--show-console-readout=false",
    ]
    if not enable_ipv6:
        # Disabling DHT6 alone would still start IPv6 RPC and BitTorrent listeners.
        args.append("--disable-ipv6=true")
    if options.bt_stop_timeout > 0:
        args.append(f"--bt-stop-timeout={options.bt_stop_timeout}")
    if options.sequential:
        args.append("--bt-prioritize-piece=head=32M,tail=4M")
    if options.selected_files:
        args.append("--select-file=" + ",".join(str(i) for i in options.selected_files))
    if options.extra_trackers and not is_private:
        # aria2 has no default bootstrap node. Public jobs can discover IPv4 DHT
        # peers on a cold worker without a pre-populated dht.dat.
        args.append("--dht-entry-point=router.bittorrent.com:6881")
        tracker_csv = merged_trackers_csv(trackers, is_private=is_private)
        if tracker_csv:
            args.append(f"--bt-tracker={tracker_csv}")
    return args


class Aria2RpcError(RuntimeError):
    """Raised when aria2c JSON-RPC returns an error payload."""


class Aria2RpcClient:
    """Lightweight async JSON-RPC 2.0 client for aria2c."""

    def __init__(self, port: int, secret: str, host: str = "127.0.0.1") -> None:
        self.url = f"http://{host}:{port}/jsonrpc"
        self.secret_token = f"token:{secret}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
        self._req_id = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, *params: Any) -> Any:
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._req_id),
            "method": method,
            "params": [self.secret_token, *params],
        }
        resp = await self._client.post(self.url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body and body["error"] is not None:
            err = body["error"]
            raise Aria2RpcError(f"{method} failed ({err.get('code')}): {err.get('message')}")
        return body.get("result")

    async def get_version(self) -> dict[str, Any]:
        return dict(await self.call("aria2.getVersion"))

    async def add_uri(self, uri: str, options: dict[str, Any] | None = None) -> str:
        return str(await self.call("aria2.addUri", [uri], options or {}))

    async def add_torrent(self, torrent_bytes: bytes, options: dict[str, Any] | None = None) -> str:
        encoded = base64.b64encode(torrent_bytes).decode("ascii")
        return str(await self.call("aria2.addTorrent", encoded, [], options or {}))

    async def tell_status(self, gid: str) -> dict[str, Any]:
        return dict(await self.call("aria2.tellStatus", gid))

    async def pause(self, gid: str) -> str:
        return str(await self.call("aria2.forcePause", gid))

    async def unpause(self, gid: str) -> str:
        return str(await self.call("aria2.unpause", gid))

    async def force_remove(self, gid: str) -> str:
        return str(await self.call("aria2.forceRemove", gid))

    async def change_option(self, gid: str, options: dict[str, str]) -> str:
        return str(await self.call("aria2.changeOption", gid, options))

    async def change_global_option(self, options: dict[str, str]) -> str:
        return str(await self.call("aria2.changeGlobalOption", options))

    async def shutdown(self) -> str:
        return str(await self.call("aria2.forceShutdown"))


def _missing_dht_tables() -> set[Path]:
    """Snapshot missing default caches before submission; don't stat paths from logs."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", ""))
    if not cache_home.is_absolute():
        try:
            cache_home = Path.home() / ".cache"
        except RuntimeError:  # No home directory; leave the logs unchanged.
            return set()
    missing: set[Path] = set()
    for name in ("dht.dat", "dht6.dat"):
        path = cache_home / "aria2" / name
        try:
            path.stat()
        except FileNotFoundError:
            missing.add(path)
        except OSError:
            pass  # Unreadable isn't the same as missing; keep aria2's diagnostic.
    return missing


def _classify_log_level(line: str) -> str:
    upper = line.upper()
    if "[ERROR]" in upper or "ERROR:" in upper or "EXCEPTION" in upper:
        return "ERROR"
    if "[WARN]" in upper or "WARNING" in upper:
        return "WARNING"
    if "[DEBUG]" in upper:
        return "DEBUG"
    return "INFO"


def parse_aria2_status(
    status_payload: dict[str, Any],
    *,
    previous_status: JobStatus,
    fallback_name: str,
) -> dict[str, Any]:
    """Normalize an `aria2.tellStatus` payload into structured job metrics."""
    raw_status = str(status_payload.get("status", "active"))
    total = int(status_payload.get("totalLength", 0) or 0)
    completed = int(status_payload.get("completedLength", 0) or 0)
    upload = int(status_payload.get("uploadLength", 0) or 0)
    dl_speed = int(status_payload.get("downloadSpeed", 0) or 0)
    ul_speed = int(status_payload.get("uploadSpeed", 0) or 0)
    connections = int(status_payload.get("connections", 0) or 0)
    seeders = int(status_payload.get("numSeeders", 0) or 0)
    seeder_flag = str(status_payload.get("seeder", "false")).lower() == "true"
    info_hash = status_payload.get("infoHash")

    bt_info = status_payload.get("bittorrent") or {}
    bt_name = (bt_info.get("info") or {}).get("name")
    name = str(bt_name).strip() if bt_name else fallback_name

    # If still resolving magnet metadata, `bt_name` may be absent and `followedBy` not yet set.
    is_metadata = bool(info_hash and not bt_name and total <= 0)

    if raw_status == "complete":
        job_status = JobStatus.COMPLETED
    elif raw_status == "error":
        job_status = JobStatus.ERROR
    elif raw_status == "paused":
        job_status = JobStatus.PAUSED
    elif raw_status == "removed":
        job_status = JobStatus.CANCELLED
    elif raw_status == "waiting":
        job_status = JobStatus.QUEUED
    elif is_metadata:
        job_status = JobStatus.METADATA
    elif seeder_flag and total > 0 and completed >= total:
        job_status = JobStatus.SEEDING
    elif previous_status == JobStatus.STARTING and total == 0 and info_hash:
        job_status = JobStatus.METADATA
    else:
        job_status = JobStatus.DOWNLOADING

    progress = round((completed / total) * 100.0, 2) if total > 0 else 0.0
    if job_status in (JobStatus.COMPLETED, JobStatus.SEEDING):
        progress = 100.0

    remaining = max(0, total - completed)
    eta = int(remaining / dl_speed) if (dl_speed > 0 and remaining > 0) else None

    files: list[TorrentFileSpec] = []
    for f in status_payload.get("files") or []:
        f_path = str(f.get("path", "")).strip()
        if f_path.startswith("[METADATA]"):
            continue
        files.append(
            TorrentFileSpec(
                index=int(f.get("index", len(files) + 1)),
                path=f_path,
                size=int(f.get("length", 0) or 0),
                completed=int(f.get("completedLength", 0) or 0),
                selected=str(f.get("selected", "true")).lower() == "true",
            )
        )

    followed_by = [str(g) for g in (status_payload.get("followedBy") or []) if g]
    error_msg = None
    if raw_status == "error":
        code = status_payload.get("errorCode", "unknown")
        msg = status_payload.get("errorMessage", "") or "Download failed"
        error_msg = f"aria2c error {code}: {msg}"

    return {
        "status": job_status,
        "name": name,
        "info_hash": str(info_hash).lower() if info_hash else None,
        "total_bytes": total,
        "completed_bytes": completed,
        "upload_bytes": upload,
        "download_speed": dl_speed,
        "upload_speed": ul_speed,
        "progress": progress,
        "eta_seconds": eta,
        "connections": connections,
        "seeders": seeders,
        "files": files,
        "followed_by": followed_by,
        "error": error_msg,
    }


OnProgressFn = Callable[[JobInfo], Awaitable[None]]
OnLogFn = Callable[[LogEvent], Awaitable[None]]
OnCommitFn = Callable[[], Awaitable[None]]


class Aria2JobSupervisor:
    """Runs aria2c for one job, handles magnet metadata transitions, logs, and live commands."""

    def __init__(
        self,
        *,
        job: JobInfo,
        output_dir: Path,
        source_uri: str | None = None,
        torrent_bytes: bytes | None = None,
        trackers: list[str] | None = None,
        is_private: bool = False,
        on_progress: OnProgressFn,
        on_log: OnLogFn,
        on_commit: OnCommitFn | None = None,
        rpc_port: int | None = None,
        poll_interval: float = 0.75,
        commit_interval: float = 20.0,
    ) -> None:
        self.job = job
        self.output_dir = output_dir
        self.source_uri = source_uri
        self.torrent_bytes = torrent_bytes
        self.trackers = trackers
        self.is_private = is_private
        self.on_progress = on_progress
        self.on_log = on_log
        self.on_commit = on_commit
        self.rpc_port = rpc_port or find_free_tcp_port()
        self.rpc_secret = secrets.token_urlsafe(18)
        self.poll_interval = poll_interval
        self.commit_interval = commit_interval

        self._seq = max((log.seq for log in job.logs), default=0)
        self._proc: asyncio.subprocess.Process | None = None
        self._rpc: Aria2RpcClient | None = None
        self._active_gid: str | None = None
        self._cmd_queue: asyncio.Queue[JobCommand] = asyncio.Queue()
        self._cancelled = False
        self._delete_on_cancel = False
        self._stop_seeding_requested = False
        self._known_paths: set[Path] = set()

    async def enqueue_command(self, cmd: JobCommand) -> None:
        await self._cmd_queue.put(cmd)

    async def _emit_log(
        self,
        message: str,
        *,
        level: str = "INFO",
        source: str = "utd-worker",
    ) -> None:
        self._seq += 1
        event = LogEvent(
            seq=self._seq,
            ts=time.time(),
            level=level,  # type: ignore[arg-type]
            source=source,
            message=message,
        )
        self.job.logs.append(event)
        if len(self.job.logs) > 300:
            self.job.logs = self.job.logs[-300:]
        await self.on_log(event)

    async def _pump_stream(self, stream: asyncio.StreamReader | None, source: str) -> None:
        if stream is None:
            return
        cold_tables = _missing_dht_tables()
        missing_table = None
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            # aria2 1.37 reports a cold cache as a two-line exception, then creates
            # its own routing table. Don't swallow other load, bind or save failures.
            if (
                missing_table is not None
                and line.startswith("Exception: [DHTRoutingTableDeserializer.cc:")
                and line.endswith(f" Failed to load DHT routing table from {missing_table}")
            ):
                missing_table = None
                continue
            _, header, path = line.partition(
                "[ERROR] Exception caught while loading DHT routing table from "
            )
            if header and Path(path) in cold_tables:
                missing_table = path
                await self._emit_log(
                    f"No saved DHT routing table at {path}; starting with an empty table.",
                    source=source,
                )
            else:
                missing_table = None
                await self._emit_log(line, level=_classify_log_level(line), source=source)

    async def _wait_for_rpc_ready(self, rpc: Aria2RpcClient) -> None:
        for _ in range(50):
            if self._proc and self._proc.returncode is not None:
                raise RuntimeError(f"aria2c exited prematurely with code {self._proc.returncode}")
            try:
                ver = await rpc.get_version()
                await self._emit_log(
                    f"aria2c {ver.get('version', 'unknown')} RPC ready on "
                    f"127.0.0.1:{self.rpc_port}",
                    source="utd-worker",
                )
                return
            except Exception:
                await asyncio.sleep(0.1)
        raise TimeoutError("Timed out waiting for aria2c JSON-RPC server to become ready.")

    async def _apply_command(self, cmd: JobCommand) -> bool:
        """Apply a command; returns True if the worker should terminate the job."""
        if not self._rpc or not self._active_gid:
            return False
        action = cmd.action
        if action == "pause":
            await self._rpc.pause(self._active_gid)
            self.job.status = JobStatus.PAUSED
            await self._emit_log("Paused download.", source="utd-worker")
            await self.on_progress(self.job)
        elif action == "resume":
            await self._rpc.unpause(self._active_gid)
            self.job.status = JobStatus.DOWNLOADING
            await self._emit_log("Resumed download.", source="utd-worker")
            await self.on_progress(self.job)
        elif action == "stop_seeding":
            self._stop_seeding_requested = True
            with contextlib.suppress(Exception):
                await self._rpc.force_remove(self._active_gid)
            await self._emit_log("Stopping seeding and finalizing files.", source="utd-worker")
            return True
        elif action == "cancel":
            self._cancelled = True
            self._delete_on_cancel = cmd.delete_files
            with contextlib.suppress(Exception):
                await self._rpc.force_remove(self._active_gid)
            await self._emit_log(
                f"Cancelled job (delete_files={cmd.delete_files}).",
                level="WARNING",
                source="utd-worker",
            )
            return True
        elif action == "update_options" and cmd.options is not None:
            patch: JobOptionsPatch = cmd.options
            rpc_opts: dict[str, str] = {}
            if patch.max_download_limit is not None:
                rpc_opts["max-overall-download-limit"] = patch.max_download_limit
                self.job.options.max_download_limit = patch.max_download_limit
            if patch.max_upload_limit is not None:
                rpc_opts["max-overall-upload-limit"] = patch.max_upload_limit
                self.job.options.max_upload_limit = patch.max_upload_limit
            if rpc_opts:
                await self._rpc.change_global_option(rpc_opts)
            if patch.max_peers is not None:
                self.job.options.max_peers = patch.max_peers
                with contextlib.suppress(Exception):
                    await self._rpc.change_option(
                        self._active_gid, {"bt-max-peers": str(patch.max_peers)}
                    )
            await self._emit_log(f"Updated runtime options: {rpc_opts}", source="utd-worker")
            await self.on_progress(self.job)
        return False

    def _cleanup_cancelled_files(self) -> None:
        for file_path in sorted(self._known_paths, reverse=True):
            with contextlib.suppress(OSError):
                if file_path.is_file():
                    file_path.unlink(missing_ok=True)
                aria_ctrl = Path(f"{file_path}.aria2")
                if aria_ctrl.is_file():
                    aria_ctrl.unlink(missing_ok=True)
        # Clean up empty subdirectories created inside output_dir
        for path in sorted(self._known_paths, reverse=True):
            parent = path.parent
            while parent != self.output_dir and str(parent).startswith(str(self.output_dir)):
                with contextlib.suppress(OSError):
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                    else:
                        break
                parent = parent.parent

    async def run(self) -> JobInfo:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.job.status = JobStatus.STARTING
        self.job.updated_at = time.time()
        await self.on_progress(self.job)

        cmd_args = build_aria2c_args(
            output_dir=self.output_dir,
            rpc_port=self.rpc_port,
            rpc_secret=self.rpc_secret,
            options=self.job.options,
            trackers=self.trackers,
            is_private=self.is_private,
            enable_ipv6=_can_listen_on_ipv6(),
        )
        await self._emit_log(f"Starting download engine in {self.output_dir}", source="utd-worker")

        self._proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        log_task = asyncio.create_task(self._pump_stream(self._proc.stdout, "aria2c"))
        self._rpc = Aria2RpcClient(self.rpc_port, self.rpc_secret)

        try:
            await self._wait_for_rpc_ready(self._rpc)

            if self.torrent_bytes is not None:
                self._active_gid = await self._rpc.add_torrent(self.torrent_bytes)
                await self._emit_log(
                    f"Submitted .torrent job (gid={self._active_gid})", source="utd-worker"
                )
            elif self.source_uri is not None:
                self._active_gid = await self._rpc.add_uri(self.source_uri)
                await self._emit_log(
                    f"Submitted URI job (gid={self._active_gid})", source="utd-worker"
                )
            else:
                raise ValueError("Job must provide either source_uri or torrent_bytes.")

            last_commit = time.monotonic()

            while True:
                # Drain pending commands before polling telemetry.
                should_stop = False
                while not self._cmd_queue.empty():
                    cmd = self._cmd_queue.get_nowait()
                    if await self._apply_command(cmd):
                        should_stop = True
                        break
                if should_stop:
                    break

                assert self._active_gid is not None
                status_payload = await self._rpc.tell_status(self._active_gid)
                parsed = parse_aria2_status(
                    status_payload,
                    previous_status=self.job.status,
                    fallback_name=self.job.name,
                )

                # Follow magnet metadata -> payload GID transition cleanly.
                followed_by: list[str] = parsed["followed_by"]
                if followed_by:
                    next_gid = followed_by[0]
                    if next_gid != self._active_gid:
                        self._active_gid = next_gid
                        await self._emit_log(
                            f"Metadata resolved; switching to payload gid={next_gid}",
                            source="utd-worker",
                        )
                        continue

                # Update JobInfo fields cleanly.
                self.job.status = parsed["status"]
                if parsed["name"]:
                    self.job.name = parsed["name"]
                if parsed["info_hash"]:
                    self.job.info_hash = parsed["info_hash"]
                self.job.total_bytes = parsed["total_bytes"]
                self.job.completed_bytes = parsed["completed_bytes"]
                self.job.upload_bytes = parsed["upload_bytes"]
                self.job.download_speed = parsed["download_speed"]
                self.job.upload_speed = parsed["upload_speed"]
                self.job.progress = parsed["progress"]
                self.job.eta_seconds = parsed["eta_seconds"]
                self.job.connections = parsed["connections"]
                self.job.seeders = parsed["seeders"]

                # Normalize file paths relative to output_dir where possible.
                normalized_files: list[TorrentFileSpec] = []
                for spec in parsed["files"]:
                    p = Path(spec.path)
                    if p.is_absolute():
                        self._known_paths.add(p)
                        try:
                            rel = p.relative_to(self.output_dir).as_posix()
                        except ValueError:
                            rel = p.name
                    else:
                        rel = spec.path
                        self._known_paths.add(self.output_dir / rel)
                    normalized_files.append(
                        TorrentFileSpec(
                            index=spec.index,
                            path=rel,
                            size=spec.size,
                            completed=spec.completed,
                            selected=spec.selected,
                        )
                    )
                self.job.files = normalized_files
                self.job.error = parsed["error"]
                self.job.updated_at = time.time()

                await self.on_progress(self.job)

                if self.job.status in (JobStatus.COMPLETED, JobStatus.ERROR, JobStatus.CANCELLED):
                    break

                now = time.monotonic()
                if self.on_commit and (now - last_commit) >= self.commit_interval:
                    try:
                        await self.on_commit()
                    except Exception as exc:
                        logger.warning("Periodic volume commit warning: %s", exc)
                    last_commit = now

                # Wait up to poll_interval, waking immediately if a command arrives.
                try:
                    cmd = await asyncio.wait_for(self._cmd_queue.get(), timeout=self.poll_interval)
                    if await self._apply_command(cmd):
                        break
                except TimeoutError:
                    pass

            if self._stop_seeding_requested:
                self.job.status = JobStatus.COMPLETED
                self.job.progress = 100.0
                self.job.download_speed = 0
                self.job.upload_speed = 0
                self.job.eta_seconds = None
            elif self._cancelled:
                self.job.status = JobStatus.CANCELLED
                self.job.download_speed = 0
                self.job.upload_speed = 0
                self.job.eta_seconds = None

        except Exception as exc:
            self.job.status = JobStatus.ERROR
            self.job.error = str(exc)
            await self._emit_log(f"Worker error: {exc}", level="ERROR", source="utd-worker")
        finally:
            if self._rpc:
                with contextlib.suppress(Exception):
                    await self._rpc.shutdown()
                await self._rpc.close()
            if self._proc:
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(log_task, timeout=2.0)

            if self._cancelled and self._delete_on_cancel:
                await asyncio.to_thread(self._cleanup_cancelled_files)

            if self.on_commit:
                with contextlib.suppress(Exception):
                    await self.on_commit()

            self.job.download_speed = 0
            self.job.upload_speed = 0
            self.job.updated_at = time.time()
            self.job.finished_at = self.job.updated_at
            await self.on_progress(self.job)

        return self.job


def remove_paths_on_disk(paths: list[Path]) -> None:
    """Helper to remove job artifacts when deleting a job with delete_files=True."""
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file():
            p.unlink(missing_ok=True)
