"""Constants and tuning defaults for the Modal cloud backend."""

from __future__ import annotations

import os

APP_NAME = os.environ.get("UTD_MODAL_APP_NAME", "ultimate-torrent-downloader")
VOLUME_NAME = os.environ.get("UTD_VOLUME_NAME", "utd-downloads")
JOBS_DICT_NAME = os.environ.get("UTD_JOBS_DICT_NAME", "utd-jobs")
LOGS_QUEUE_NAME = os.environ.get("UTD_LOGS_QUEUE_NAME", "utd-logs")
CONTROL_QUEUE_NAME = os.environ.get("UTD_CONTROL_QUEUE_NAME", "utd-control")
AUTH_SECRET_NAME = os.environ.get("UTD_AUTH_SECRET_NAME", "utd-auth")

VOLUME_MOUNT_PATH = "/vol"
DOWNLOADS_ROOT_REL = "downloads"

MAX_STORED_LOGS_PER_JOB = 300
VOLUME_COMMIT_INTERVAL_SECONDS = 20.0
WORKER_TIMEOUT_SECONDS = 86400  # 24 hours (Modal Function max timeout)
GATEWAY_SCALEDOWN_SECONDS = 300
