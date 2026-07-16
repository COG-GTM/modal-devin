"""Claim, dispatch, and session lifecycle orchestration."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

import modal
from modal.stream_type import StreamType

from modal_devin._client import ClaimConflict, OutpostsClient, SessionStatus
from modal_devin._config import (
    WorkerConfig,
    WorkerSettings,
    _sandbox_lifetime_seconds,
    _session_function_timeout_seconds,
)
from modal_devin._exceptions import (
    ModalCompatibilityError,
    OutpostsAPIError,
    OutpostsProtocolError,
    SessionStatusUnknownError,
    WorkerExitedError,
)
from modal_devin.images import (
    _DEVIN_BIN,
    _DUMMY_TOKEN,
    _SIDECAR_PORT,
    _SIDECAR_RECIPE_DIGEST,
    _build_sidecar_image_id,
    _create_sidecar,
)

logger = logging.getLogger("modal_devin.worker")

WORKDIR = "/root/workspace"
_SIDECAR_IMAGE_KEY = f"__modal_devin_sidecar_image_id__:{_SIDECAR_RECIPE_DIGEST}"
_TERMINAL_STATUSES = {
    SessionStatus.EXIT,
    SessionStatus.ERROR,
    SessionStatus.TERMINATED,
}
_SNAPSHOT_KEY_PREFIX = "__modal_devin_snapshot__:"
_DISPATCH_KEY_PREFIX = "__modal_devin_dispatch__:"
_SNAPSHOT_INDEX_REFRESH_KEY = "__modal_devin_snapshot_index_refreshed_at__"
_SNAPSHOT_INDEX_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60


class SnapshotStore(Protocol):
    def get(self, key: str, default: object = None) -> object: ...

    def put(self, key: str, value: object, *, skip_if_exists: bool = False) -> bool: ...

    def pop(self, key: str, default: object = ...) -> object: ...

    def keys(self) -> Iterable[object]: ...


def _session_store_key(prefix: str, session_id: str) -> str:
    return prefix + hashlib.sha256(session_id.encode()).hexdigest()


def _snapshot_key(session_id: str) -> str:
    return _session_store_key(_SNAPSHOT_KEY_PREFIX, session_id)


def _dispatch_key(session_id: str) -> str:
    return _session_store_key(_DISPATCH_KEY_PREFIX, session_id)


def _refresh_snapshot_index(snapshot_store: SnapshotStore) -> None:
    """Keep the Modal Dict index alive for snapshots with longer retention."""
    now = time.time()
    last_refresh = snapshot_store.get(_SNAPSHOT_INDEX_REFRESH_KEY)
    if isinstance(last_refresh, (int, float)) and (
        now - last_refresh < _SNAPSHOT_INDEX_REFRESH_INTERVAL_SECONDS
    ):
        return

    for key in snapshot_store.keys():  # noqa: SIM118 - remote Dict is not directly iterable
        if isinstance(key, str) and key.startswith(_SNAPSHOT_KEY_PREFIX):
            snapshot_store.get(key)
    snapshot_store.put(_SNAPSHOT_INDEX_REFRESH_KEY, now)


def _reserve_dispatch(
    snapshot_store: SnapshotStore,
    session_id: str,
    settings: WorkerSettings,
) -> bool:
    """Acquire a bounded queueing lease for one session dispatch."""
    key = _dispatch_key(session_id)
    now = time.time()
    current_expiry = snapshot_store.get(key)
    if isinstance(current_expiry, (int, float)) and current_expiry > now:
        return False
    if current_expiry is not None:
        snapshot_store.pop(key, None)
    return snapshot_store.put(
        key,
        now + _session_function_timeout_seconds(settings),
        skip_if_exists=True,
    )


def _configure_logging(settings: WorkerSettings) -> None:
    """Apply the worker log level without changing the application's root logger."""
    logger.setLevel(settings.log_level.upper())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False


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
        timeout=_sandbox_lifetime_seconds(settings),
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
    snapshot_key = _snapshot_key(session_id)
    snapshot_id = snapshot_store.get(snapshot_key)
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
            snapshot_store.pop(snapshot_key, None)
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


def _wait_for_sidecar(sandbox: modal.Sandbox, *, timeout_seconds: int) -> None:
    attempts = max(1, math.ceil(timeout_seconds / 0.2))
    process = sandbox.exec(
        "sh",
        "-c",
        f"for i in $(seq 1 {attempts}); do "
        f"curl -fsS http://caddy:{_SIDECAR_PORT}/_modal_devin/health -o /dev/null "
        f"&& exit 0; sleep 0.2; done; "
        f"curl -v http://caddy:{_SIDECAR_PORT}/_modal_devin/health 2>&1; exit 1",
    )
    if process.wait() != 0:
        raise RuntimeError(
            f"Caddy sidecar did not become ready within {timeout_seconds} seconds:\n"
            + process.stdout.read()
        )


def _final_status(
    client: OutpostsClient,
    *,
    session_id: str,
    acceptor_id: str,
    settings: WorkerSettings,
) -> tuple[bool, SessionStatus | None]:
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
    image = sandbox.snapshot_filesystem(
        timeout=settings.snapshot_timeout_seconds,
        ttl=settings.snapshot_ttl_seconds,
    )
    image_id = image.object_id
    if image_id is None:
        raise RuntimeError("Modal returned a filesystem snapshot without an object ID")
    snapshot_store.put(_snapshot_key(session_id), image_id)
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
    connect_token: str | None,
    gateway_url: str | None,
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
        _wait_for_sidecar(
            sandbox,
            timeout_seconds=settings.sidecar_ready_timeout_seconds,
        )

        exec_env: dict[str, str | None] = {
            "DEVIN_API_URL": f"http://caddy:{_SIDECAR_PORT}",
            "DEVIN_OUTPOSTS_TOKEN": _DUMMY_TOKEN,
        }
        if connect_token is not None:
            # Already claimed via our own client.claim() call above -- handing the resulting
            # connect_token to devin-cli makes it run the remote directly instead of claiming
            # the session a second time itself (which the beta API's response shape breaks).
            exec_env["DEVIN_REMOTE_SESSION_TOKEN"] = connect_token
            if gateway_url is not None:
                # devin-cli only learns the gateway URL from the claim response it would have
                # made itself; since DEVIN_REMOTE_SESSION_TOKEN skips that call, it must be
                # supplied directly here instead.
                exec_env["DEVIN_OUTPOST_GATEWAY_URL"] = gateway_url
        process = sandbox.exec(
            _DEVIN_BIN,
            "worker",
            "start",
            "--session",
            session_id,
            "--pool",
            config.outpost_id,
            "--acceptor-id",
            config.acceptor_id,
            env=exec_env,
            stderr=StreamType.STDOUT,
            timeout=settings.session_timeout_seconds,
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
        safe_terminal = status is None or status in _TERMINAL_STATUSES
        if not known or (status != SessionStatus.SUSPENDED and not safe_terminal):
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

        if status == SessionStatus.SUSPENDED:
            _snapshot(
                sandbox,
                session_id=session_id,
                snapshot_store=snapshot_store,
                settings=settings,
            )
        else:
            snapshot_store.pop(_snapshot_key(session_id), None)

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
    _configure_logging(settings)
    client = OutpostsClient(
        config.api_url,
        token,
        timeout=settings.api_timeout_seconds,
    )
    snapshot_store = modal.Dict.from_name(
        config.snapshot_store_name,
        create_if_missing=True,
    )
    snapshot_store.pop(_dispatch_key(session_id), None)
    try:
        claim = client.claim(session_id, config.acceptor_id)
    except ClaimConflict:
        logger.info("[%s] claim was acquired by another invocation", session_id)
        return
    logger.info(
        "[%s] claimed with deadline %s; starting worker lifecycle",
        session_id,
        claim.deadline,
    )

    try:
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
            connect_token=claim.connect_token,
            gateway_url=claim.gateway_url,
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
    """Discover pending sessions and dispatch one Modal function call per session."""
    _configure_logging(settings)
    snapshot_store = modal.Dict.from_name(
        config.snapshot_store_name,
        create_if_missing=True,
    )
    _refresh_snapshot_index(snapshot_store)
    client = OutpostsClient(
        config.api_url,
        token,
        timeout=settings.api_timeout_seconds,
    )
    try:
        pending = client.pending_session_ids(config.outpost_id)
    except OutpostsProtocolError:
        logger.exception("Outposts scheduler received an invalid response")
        raise
    except OutpostsAPIError:
        logger.exception("Outposts scheduler request failed")
        raise
    if not pending:
        return

    sidecar_image_id = snapshot_store.get(_SIDECAR_IMAGE_KEY)
    if not isinstance(sidecar_image_id, str):
        try:
            sidecar_image_id = _build_sidecar_image_id(config.image_build_app_name)
            snapshot_store.put(_SIDECAR_IMAGE_KEY, sidecar_image_id)
        except Exception:
            logger.exception("sidecar image build failed before dispatching sessions")
            raise

    dispatch_failures: list[Exception] = []
    for session_id in pending:
        if not _reserve_dispatch(snapshot_store, session_id, settings):
            logger.info("[%s] an invocation is already queued", session_id)
            continue
        try:
            spawn_session(session_id)
        except Exception as error:
            snapshot_store.pop(_dispatch_key(session_id), None)
            logger.exception("[%s] dispatch failed", session_id)
            error.add_note(f"while dispatching session {session_id!r}")
            dispatch_failures.append(error)

    if dispatch_failures:
        raise ExceptionGroup("one or more session dispatches failed", dispatch_failures)
