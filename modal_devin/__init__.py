"""Run Devin Outposts workers on Modal."""

from importlib.metadata import PackageNotFoundError, version

from modal_devin._config import WorkerSettings
from modal_devin._exceptions import (
    ClaimDeadlineError,
    ConfigurationError,
    ModalCompatibilityError,
    ModalDevinError,
    OutpostsAPIError,
    OutpostsProtocolError,
    SessionAttachTimeoutError,
    SessionStatusUnknownError,
    WorkerExitedError,
)
from modal_devin.worker import Worker

try:
    __version__ = version("modal-devin")
except PackageNotFoundError:  # pragma: no cover - source trees without installed metadata
    __version__ = "0+unknown"

__all__ = [
    "ClaimDeadlineError",
    "ConfigurationError",
    "ModalCompatibilityError",
    "ModalDevinError",
    "OutpostsAPIError",
    "OutpostsProtocolError",
    "SessionAttachTimeoutError",
    "SessionStatusUnknownError",
    "Worker",
    "WorkerExitedError",
    "WorkerSettings",
    "__version__",
]
