"""Logfire-backed OpenTelemetry setup and distributed context helpers."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, cast

import logfire

from modal_devin._config import WorkerConfig

_configure_lock = threading.Lock()
_configured = False


def _service_version() -> str:
    try:
        return version("modal-devin")
    except PackageNotFoundError:  # pragma: no cover - Modal source mount without metadata
        return "0+unknown"


# Identifier attributes (not credentials) that Logfire's default scrubber would
# otherwise redact because their names match patterns like "session".
_SCRUBBING_EXEMPT_ATTRIBUTES = {"devin.session.id"}


def _keep_identifier_attributes(match: logfire.ScrubMatch) -> object:
    if match.path and match.path[-1] in _SCRUBBING_EXEMPT_ATTRIBUTES:
        return match.value
    return None


def configure_observability(config: WorkerConfig) -> None:
    """Configure Logfire once per Modal container.

    A token enables the Pydantic Logfire backend. Without one, Logfire remains a
    standards-compatible OpenTelemetry SDK and honors OTLP exporter environment
    variables without making Logfire a required service.
    """
    global _configured
    if _configured:
        return
    with _configure_lock:
        if _configured:
            return
        logfire.configure(
            send_to_logfire="if-token-present",
            service_name=config.app_name,
            service_version=_service_version(),
            console=False,
            distributed_tracing=True,
            scrubbing=logfire.ScrubbingOptions(callback=_keep_identifier_attributes),
        )
        _configured = True


@contextmanager
def span(
    name: str,
    *,
    config: WorkerConfig,
    session_id: str | None = None,
    attributes: Mapping[str, object] | None = None,
) -> Iterator[None]:
    """Create a consistently attributed modal-devin span."""
    if not _configured:
        yield
        return
    span_attributes: dict[str, object] = {
        "modal_devin.worker.name": config.name,
        "modal_devin.outpost.id": config.outpost_id,
    }
    if attributes is not None:
        span_attributes.update(attributes)
    if session_id is not None:
        span_attributes["devin.session.id"] = session_id
    with logfire.span(name, _span_name=name, **cast(dict[str, Any], span_attributes)):
        yield


def get_context() -> dict[str, str]:
    """Serialize the active W3C OpenTelemetry context for a remote invocation."""
    if not _configured:
        return {}
    return dict(logfire.get_context())


@contextmanager
def attach_context(carrier: Mapping[str, str] | None) -> Iterator[None]:
    """Make a scheduler trace carrier current while a session invocation runs."""
    if not _configured or not carrier:
        yield
        return
    with logfire.attach_context(dict(carrier)):
        yield
