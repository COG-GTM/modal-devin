"""Public exception hierarchy for :mod:`modal_devin`."""

from __future__ import annotations


class ModalDevinError(Exception):
    """Base class for errors raised by modal-devin."""


class ConfigurationError(ModalDevinError, ValueError):
    """The worker configuration is invalid."""


class ModalCompatibilityError(ModalDevinError):
    """The installed Modal SDK does not provide a required capability."""


class OutpostsAPIError(ModalDevinError):
    """A Devin Outposts API request failed."""


class OutpostsProtocolError(OutpostsAPIError):
    """The Devin Outposts API returned an unexpected response."""


class SessionStatusUnknownError(ModalDevinError):
    """A session's final status could not be established safely."""


class WorkerExitedError(ModalDevinError):
    """The Devin worker process exited unsuccessfully."""

    def __init__(self, session_id: str, returncode: int) -> None:
        self.session_id = session_id
        self.returncode = returncode
        super().__init__(f"Devin worker for {session_id!r} exited with status {returncode}")
