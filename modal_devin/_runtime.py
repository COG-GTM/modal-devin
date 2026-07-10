"""Claim, dispatch, and session lifecycle orchestration."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import modal
from modal.stream_type import StreamType

from modal_devin._client import ClaimConflict, OutpostsClient
from modal_devin._config import WorkerConfig, WorkerSettings
from modal_devin._exceptions import (
    ModalCompatibilityError,
    OutpostsAPIError,
    SessionStatusUnknownError,
    WorkerExitedError,
)
from modal_devin.images import (
    _DEVIN_BIN,
    _DUMMY_TOKEN,
    _SIDECAR_PORT,
    _build_sidecar_image_id,
    _create_sidecar,
)

logger = logging.getLogger("modal_devin.worker")

WORKDIR = "/root/workspace"
_SIDECAR_IMAGE_KEY = "__modal_devin_sidecar_image_id__"
_ACTIVE_STATUSES = {"pending", "claimed", "running", "resuming"}


class SnapshotStore(Protocol):
    def get(self, key: str, default: object = None) -> object: ...

    def put(self, key: str, value: object, *, skip_if_exists: bool = False) -> bool: ...

    def pop(self, key: str, default: object = ...) -> object: ...


def _release_safely(client: OutpostsClient, session_id: str, acceptor_id: str, reason: str) -> None:
    try:
        client.release(session_id, acceptor_id)
    except OutpostsAPIError as error:
        logger.warning("[%s] failed to release claim after %s: %s", session_id, reason, error)
    else:
        logger.info("[%s] released claim after %s", session_id, reason)


def _new_sandbox(
    *,
    app: modal.App,
    image: modal.Image,
    settings: WorkerSettings,
    sandbox_options: Mapping[str, Any],
) -> modal.Sandbox:
    sandbox = modal.Sandbox.create(
        "sleep",
        "infinity",
        app=app,
        image=image,
        workdir=WORKDIR,
        timeout=settings.session_timeout_seconds,
        readiness_probe=modal.sandbox.Probe.with_exec("test", "-d", WORKDIR),
        **sandbox_options,
    )
    try:
        sandbox.wait_until_ready(timeout=settings.sandbox_ready_timeout_seconds)
    except BaseException:
        _terminate_failed_sandbox(sandbox)
        raise
    return sandbox


def _terminate_failed_sandbox(sandbox: modal.Sandbox) -> None:
    try:
        sandbox.terminate()
    except Exception:
        logger.debug("failed to terminate unusable Sandbox", exc_info=True)


def _create_sandbox(
    *,
    app: modal.App,
    base_image: modal.Image,
    session_id: str,
    snapshot_store: SnapshotStore,
    settings: WorkerSettings,
    sandbox_options: Mapping[str, Any],
) -> modal.Sandbox:
    snapshot_id = snapshot_store.get(session_id)
    if isinstance(snapshot_id, str):
        try:
            sandbox = _new_sandbox(
                app=app,
                image=modal.Image.from_id(snapshot_id),
                settings=settings,
                sandbox_options=sandbox_options,
            )
            logger.info("[%s] resumed from filesystem snapshot %s", session_id, snapshot_id)
            return sandbox
        except modal.exception.NotFoundError:
            snapshot_store.pop(session_id, None)
            logger.warning(
                "[%s] snapshot %s expired; starting from the base image",
                session_id,
                snapshot_id,
            )

    return _new_sandbox(
        app=app,
        image=base_image,
        settings=settings,
        sandbox_options=sandbox_options,
    )


def _wait_for_sidecar(sandbox: modal.Sandbox) -> None:
    process = sandbox.exec(
        "sh",
        "-c",
        f"for i in $(seq 1 300); do "
        f"curl -fsS http://caddy:{_SIDECAR_PORT}/_modal_devin/health -o /dev/null "
        f"&& exit 0; sleep 0.2; done; "
        f"curl -v http://caddy:{_SIDECAR_PORT}/_modal_devin/health 2>&1; exit 1",
    )
    if process.wait() != 0:
        raise RuntimeError("Caddy sidecar did not become ready:\n" + process.stdout.read())


def _final_status(
    client: OutpostsClient,
    *,
    session_id: str,
    acceptor_id: str,
    settings: WorkerSettings,
) -> tuple[bool, str | None]:
    """Return ``(known, status)`` without collapsing request failure into absence."""
    for attempt in range(1, settings.status_attempts + 1):
        try:
            return True, client.session_status(session_id, acceptor_id)
        except OutpostsAPIError as error:
            logger.warning(
                "[%s] status lookup %s/%s failed: %s",
                session_id,
                attempt,
                settings.status_attempts,
                error,
            )
            if attempt < settings.status_attempts:
                time.sleep(settings.status_retry_delay_seconds * attempt)
    return False, None


def _snapshot(
    sandbox: modal.Sandbox,
    *,
    session_id: str,
    snapshot_store: SnapshotStore,
    settings: WorkerSettings,
) -> None:
    image = sandbox.snapshot_filesystem(ttl=settings.snapshot_ttl_seconds)
    image_id = image.object_id
    if image_id is None:
        raise RuntimeError("Modal returned a filesystem snapshot without an object ID")
    snapshot_store.put(session_id, image_id)
    logger.info("[%s] stored filesystem snapshot %s", session_id, image_id)


def _run_claimed_session(
    *,
    app: modal.App,
    image: modal.Image,
    config: WorkerConfig,
    settings: WorkerSettings,
    client: OutpostsClient,
    snapshot_store: SnapshotStore,
    session_id: str,
    sidecar_image_id: str,
    sandbox_options: Mapping[str, Any],
) -> None:
    sandbox: modal.Sandbox | None = None
    primary_error: BaseException | None = None
    try:
        sandbox = _create_sandbox(
            app=app,
            base_image=image,
            session_id=session_id,
            snapshot_store=snapshot_store,
            settings=settings,
            sandbox_options=sandbox_options,
        )
        try:
            _create_sidecar(
                sandbox,
                sidecar_image_id=sidecar_image_id,
                api_url=config.api_url,
                token=client.token,
            )
        except modal.exception.NotFoundError:
            snapshot_store.pop(_SIDECAR_IMAGE_KEY, None)
            raise
        _wait_for_sidecar(sandbox)

        process = sandbox.exec(
            _DEVIN_BIN,
            "worker",
            "start",
            "--session",
            session_id,
            "--pool",
            config.pool_id,
            "--acceptor-id",
            config.acceptor_id,
            env={
                "DEVIN_API_URL": f"http://caddy:{_SIDECAR_PORT}",
                "DEVIN_OUTPOSTS_TOKEN": _DUMMY_TOKEN,
            },
            stderr=StreamType.STDOUT,
        )
        for line in process.stdout:
            logger.info("[%s] %s", session_id, line.rstrip())
        returncode = process.wait()
        logger.info("[%s] Devin worker exited with status %s", session_id, returncode)

        known, status = _final_status(
            client,
            session_id=session_id,
            acceptor_id=config.acceptor_id,
            settings=settings,
        )
        if not known or status in _ACTIVE_STATUSES:
            logger.warning(
                "[%s] final session status is unsafe (%s); preserving a recovery snapshot",
                session_id,
                status if known else "unavailable",
            )
            _snapshot(
                sandbox,
                session_id=session_id,
                snapshot_store=snapshot_store,
                settings=settings,
            )
            raise SessionStatusUnknownError(
                f"could not establish a safe final state for session {session_id!r}"
            )

        if status == "suspended":
            _snapshot(
                sandbox,
                session_id=session_id,
                snapshot_store=snapshot_store,
                settings=settings,
            )
        else:
            snapshot_store.pop(session_id, None)

        if returncode != 0:
            raise WorkerExitedError(session_id, returncode)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate()
            except Exception:
                if primary_error is None:
                    raise
                logger.exception("[%s] Sandbox termination also failed", session_id)


def execute_session(
    *,
    app: modal.App,
    image: modal.Image,
    config: WorkerConfig,
    settings: WorkerSettings,
    session_id: str,
    token: str,
    sandbox_options: Mapping[str, Any],
) -> None:
    """Run one dispatched session and release it if any lifecycle step fails."""
    logging.basicConfig(level=settings.log_level.upper())
    client = OutpostsClient(
        config.api_url,
        token,
        timeout=settings.api_timeout_seconds,
    )
    try:
        snapshot_store = modal.Dict.from_name(
            config.snapshot_store_name,
            create_if_missing=True,
        )
        sidecar_image_id = snapshot_store.get(_SIDECAR_IMAGE_KEY)
        if not isinstance(sidecar_image_id, str):
            raise ModalCompatibilityError("worker sidecar image has not been prepared")
        _run_claimed_session(
            app=app,
            image=image,
            config=config,
            settings=settings,
            client=client,
            snapshot_store=snapshot_store,
            session_id=session_id,
            sidecar_image_id=sidecar_image_id,
            sandbox_options=sandbox_options,
        )
    except BaseException:
        logger.exception("[%s] worker lifecycle failed", session_id)
        _release_safely(client, session_id, config.acceptor_id, "worker failure")
        raise


def dispatch_pending_sessions(
    *,
    config: WorkerConfig,
    settings: WorkerSettings,
    spawn_session: Callable[[str], object],
    token: str,
) -> None:
    """Claim pending sessions and dispatch one Modal function call per claim."""
    logging.basicConfig(level=settings.log_level.upper())
    snapshot_store = modal.Dict.from_name(
        config.snapshot_store_name,
        create_if_missing=True,
    )
    client = OutpostsClient(
        config.api_url,
        token,
        timeout=settings.api_timeout_seconds,
    )
    try:
        pending = client.pending_session_ids(config.pool_id)
    except OutpostsAPIError as error:
        logger.warning("Outposts scheduler request failed: %s", error)
        return
    if not pending:
        return

    sidecar_image_id = snapshot_store.get(_SIDECAR_IMAGE_KEY)
    if not isinstance(sidecar_image_id, str):
        try:
            sidecar_image_id = _build_sidecar_image_id(config.image_build_app_name)
            snapshot_store.put(_SIDECAR_IMAGE_KEY, sidecar_image_id)
        except Exception:
            logger.exception("sidecar image build failed before claiming sessions")
            return

    for session_id in pending:
        try:
            claim = client.claim(session_id, config.acceptor_id)
        except ClaimConflict:
            continue
        except OutpostsAPIError as error:
            logger.warning("[%s] claim failed: %s", session_id, error)
            continue

        logger.info(
            "[%s] claimed with deadline %s; dispatching",
            session_id,
            claim.deadline,
        )
        try:
            spawn_session(session_id)
        except Exception:
            logger.exception("[%s] dispatch failed", session_id)
            _release_safely(client, session_id, config.acceptor_id, "dispatch failure")
