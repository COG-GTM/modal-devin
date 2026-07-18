from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import modal
import pytest

from modal_devin import (
    ClaimDeadlineError,
    ModalCompatibilityError,
    OutpostsAPIError,
    OutpostsProtocolError,
    SessionStatusUnknownError,
    WorkerExitedError,
)
from modal_devin import (
    _runtime as runtime,
)
from modal_devin._client import Claim, ClaimConflict, OutpostsClient, PendingSession, SessionStatus
from modal_devin._config import WorkerConfig, WorkerSettings


class Store:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def put(self, key, value, *, skip_if_exists=False):
        if skip_if_exists and key in self.values:
            return False
        self.values[key] = value
        return True

    def pop(self, key, default=...):
        if default is ...:
            return self.values.pop(key)
        return self.values.pop(key, default)

    def keys(self):
        return iter(tuple(self.values))


class Process:
    def __init__(self, returncode=0, output=("worker output\n",)):
        self.returncode = returncode
        self.stdout = iter(output)

    def wait(self):
        return self.returncode


class Sandbox:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.terminated = False
        self.exec_calls = []
        self.snapshot_calls = []

    def exec(self, *args, **kwargs):
        self.exec_calls.append((args, kwargs))
        return Process(self.returncode)

    def snapshot_filesystem(self, *, timeout, ttl):
        self.snapshot_calls.append((timeout, ttl))
        return SimpleNamespace(object_id="im-snapshot")

    def terminate(self):
        self.terminated = True


class Client:
    token = "real-token"

    def __init__(
        self,
        *,
        status=None,
        statuses=(),
        status_errors=(),
        claim_error=None,
        claim_remote_sha=None,
        entry_remote_sha=None,
    ):
        self.status = status
        self.statuses = list(statuses)
        self.status_errors = list(status_errors)
        self.claim_error = claim_error
        self.claim_remote_sha = claim_remote_sha
        self.entry_remote_sha = entry_remote_sha
        self.claims = []
        self.releases = []

    def claim(self, session_id, acceptor_id):
        self.claims.append((session_id, acceptor_id))
        if self.claim_error:
            raise self.claim_error
        return Claim(None, "connect-token", "wss://gateway.example", self.claim_remote_sha)

    def remote_binary_sha(self, session_id):
        return self.entry_remote_sha

    def session_status(self, session_id):
        if self.status_errors:
            raise self.status_errors.pop(0)
        if self.statuses:
            return self.statuses.pop(0)
        return self.status

    def release(self, session_id, acceptor_id):
        self.releases.append((session_id, acceptor_id))


@pytest.fixture
def config():
    return WorkerConfig("demo", "outpost_env-demo", "https://api.example.com")


@pytest.fixture
def settings():
    return WorkerSettings(
        status_attempts=3,
        status_retry_delay_seconds=0,
        snapshot_ttl_seconds=123,
    )


def run_claimed(monkeypatch, *, config, settings, client, store, sandbox, remote_binary_sha=None):
    monkeypatch.setattr(runtime, "_create_sandbox", Mock(return_value=sandbox))
    monkeypatch.setattr(runtime, "_create_sidecar", Mock())
    monkeypatch.setattr(runtime, "_wait_for_sidecar", Mock())
    runtime._run_claimed_session(
        app=Mock(),
        image=Mock(),
        config=config,
        settings=settings,
        client=client,
        snapshot_store=store,
        session_id="devin-1",
        acceptor_id="modal-demo-attempt",
        claim_deadline=None,
        connect_token="connect-token",
        gateway_url="wss://gateway.example",
        remote_binary_sha=remote_binary_sha,
        sidecar_image_id="im-sidecar",
        sandbox_options={},
    )


def test_worker_logging_does_not_reconfigure_the_application_root_logger():
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    original_worker_level = runtime.logger.level
    configured = WorkerSettings(log_level="DEBUG")

    try:
        runtime._configure_logging(configured)

        assert root_logger.level == original_root_level
        assert runtime.logger.level == logging.DEBUG
    finally:
        runtime.logger.setLevel(original_worker_level)


def test_suspended_session_is_snapshotted_by_id(monkeypatch, config, settings):
    store = Store()
    sandbox = Sandbox()

    run_claimed(
        monkeypatch,
        config=config,
        settings=settings,
        client=Client(status="suspended"),
        store=store,
        sandbox=sandbox,
    )

    assert store.values[runtime._snapshot_key("devin-1")] == "im-snapshot"
    assert sandbox.snapshot_calls == [(settings.snapshot_timeout_seconds, 123)]
    assert sandbox.terminated
    [(args, kwargs)] = sandbox.exec_calls
    assert args == (
        runtime._DEVIN_BIN,
        "worker",
        "start",
        "--session",
        "devin-1",
        "--pool",
        config.outpost_id,
        "--acceptor-id",
        "modal-demo-attempt",
    )
    assert kwargs["env"] == {
        "DEVIN_API_URL": f"http://caddy:{runtime._SIDECAR_PORT}",
        "DEVIN_OUTPOSTS_TOKEN": runtime._DUMMY_TOKEN,
        "DEVIN_REMOTE_SESSION_TOKEN": "connect-token",
        "DEVIN_OUTPOST_GATEWAY_URL": "wss://gateway.example",
    }
    assert kwargs["stderr"].name == "STDOUT"
    assert kwargs["timeout"] == settings.session_timeout_seconds


def test_completed_session_removes_old_snapshot(monkeypatch, config, settings):
    store = Store({runtime._snapshot_key("devin-1"): "im-old"})

    run_claimed(
        monkeypatch,
        config=config,
        settings=settings,
        client=Client(status=None),
        store=store,
        sandbox=Sandbox(),
    )

    assert runtime._snapshot_key("devin-1") not in store.values


def test_nonzero_worker_exit_is_a_failed_modal_invocation(monkeypatch, config, settings):
    with pytest.raises(WorkerExitedError) as exc_info:
        run_claimed(
            monkeypatch,
            config=config,
            settings=settings,
            client=Client(status=None),
            store=Store(),
            sandbox=Sandbox(returncode=17),
        )

    assert exc_info.value.returncode == 17


def test_status_transport_failure_preserves_snapshot_then_fails(monkeypatch, config, settings):
    store = Store()
    sandbox = Sandbox()
    errors = [OutpostsAPIError("offline")] * settings.status_attempts

    with pytest.raises(SessionStatusUnknownError):
        run_claimed(
            monkeypatch,
            config=config,
            settings=settings,
            client=Client(status_errors=errors),
            store=store,
            sandbox=sandbox,
        )

    assert store.values[runtime._snapshot_key("devin-1")] == "im-snapshot"
    assert sandbox.terminated


@pytest.mark.parametrize(
    "status",
    ["new", "pending", "claimed", "running", "resuming", "future-state"],
)
def test_nonterminal_or_unknown_status_is_preserved_for_recovery(
    monkeypatch, config, settings, status
):
    store = Store()

    with pytest.raises(SessionStatusUnknownError):
        run_claimed(
            monkeypatch,
            config=config,
            settings=settings,
            client=Client(status=status),
            store=store,
            sandbox=Sandbox(),
        )

    assert store.values[runtime._snapshot_key("devin-1")] == "im-snapshot"


@pytest.mark.parametrize("status", ["exit", "error", "terminated"])
def test_known_terminal_status_removes_old_snapshot(monkeypatch, config, settings, status):
    store = Store({runtime._snapshot_key("devin-1"): "im-old"})

    run_claimed(
        monkeypatch,
        config=config,
        settings=settings,
        client=Client(status=status),
        store=store,
        sandbox=Sandbox(),
    )

    assert runtime._snapshot_key("devin-1") not in store.values


def test_run_session_releases_claim_after_any_failure(monkeypatch, config, settings):
    client = Client()
    dispatch_key = runtime._dispatch_key("devin-1")
    store = Store(
        {
            runtime._SIDECAR_IMAGE_KEY: "im-sidecar",
            dispatch_key: 9_999_999.0,
        }
    )
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    monkeypatch.setattr(
        runtime,
        "_run_claimed_session",
        Mock(side_effect=RuntimeError("sandbox unavailable")),
    )

    with pytest.raises(RuntimeError, match="sandbox unavailable"):
        runtime.execute_session(
            app=Mock(),
            image=Mock(),
            config=config,
            settings=settings,
            session_id="devin-1",
            token="token",
            sandbox_options={},
        )

    [(claimed_session, claimed_acceptor)] = client.claims
    assert claimed_session == "devin-1"
    assert claimed_acceptor.startswith(config.acceptor_id + "-")
    assert client.releases == [("devin-1", claimed_acceptor)]
    assert dispatch_key not in store.values


def test_run_session_releases_claim_after_success(monkeypatch, config, settings):
    client = Client()
    store = Store(
        {
            runtime._SIDECAR_IMAGE_KEY: "im-sidecar",
            runtime._dispatch_key("devin-1"): 9_999_999.0,
        }
    )
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    monkeypatch.setattr(runtime, "_run_claimed_session", Mock())
    attach_context = Mock(wraps=runtime.observability.attach_context)
    monkeypatch.setattr(runtime.observability, "attach_context", attach_context)

    runtime.execute_session(
        app=Mock(),
        image=Mock(),
        config=config,
        settings=settings,
        session_id="devin-1",
        token="token",
        sandbox_options={},
    )

    [(claimed_session, acceptor_id)] = client.claims
    assert claimed_session == "devin-1"
    assert client.releases == [("devin-1", acceptor_id)]
    assert runtime._dispatch_key("devin-1") not in store.values
    attach_context.assert_called_once_with(None)


def test_run_session_claim_conflict_is_a_successful_duplicate(monkeypatch, config, settings):
    client = Client(claim_error=ClaimConflict("devin-1"))
    run_claimed_session = Mock()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=Store()))
    monkeypatch.setattr(runtime, "_run_claimed_session", run_claimed_session)

    runtime.execute_session(
        app=Mock(),
        image=Mock(),
        config=config,
        settings=settings,
        session_id="devin-1",
        token="token",
        sandbox_options={},
    )

    [(claimed_session, claimed_acceptor)] = client.claims
    assert claimed_session == "devin-1"
    assert claimed_acceptor.startswith(config.acceptor_id + "-")
    assert client.releases == []
    run_claimed_session.assert_not_called()


def test_started_invocation_clears_its_lease_before_claiming(monkeypatch, config, settings):
    client = Client(claim_error=OutpostsAPIError("offline"))
    dispatch_key = runtime._dispatch_key("devin-1")
    store = Store({dispatch_key: 9_999_999.0})
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))

    with pytest.raises(OutpostsAPIError, match="offline"):
        runtime.execute_session(
            app=Mock(),
            image=Mock(),
            config=config,
            settings=settings,
            session_id="devin-1",
            token="token",
            sandbox_options={},
        )

    assert dispatch_key not in store.values


def test_direct_modal_retry_reclaims_with_a_new_acceptor(monkeypatch, config, settings):
    client = Client()
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    monkeypatch.setattr(
        runtime,
        "_run_claimed_session",
        Mock(side_effect=RuntimeError("retryable failure")),
    )
    for _ in range(2):
        with pytest.raises(RuntimeError, match="retryable failure"):
            runtime.execute_session(
                app=Mock(),
                image=Mock(),
                config=config,
                settings=settings,
                session_id="devin-1",
                token="token",
                sandbox_options={},
            )

    acceptors = [acceptor_id for _session_id, acceptor_id in client.claims]
    assert len(set(acceptors)) == 2
    assert all(value.startswith(config.acceptor_id + "-") for value in acceptors)
    assert client.releases == [("devin-1", value) for value in acceptors]


def test_run_session_releases_claim_when_sidecar_preparation_is_missing(
    monkeypatch, config, settings
):
    client = Client()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=Store()))

    with pytest.raises(ModalCompatibilityError, match="not been prepared"):
        runtime.execute_session(
            app=Mock(),
            image=Mock(),
            config=config,
            settings=settings,
            session_id="devin-1",
            token="token",
            sandbox_options={},
        )

    [(released_session, released_acceptor)] = client.releases
    assert released_session == "devin-1"
    assert released_acceptor.startswith(config.acceptor_id + "-")


def test_lazy_snapshot_expiry_falls_back_and_forgets_mapping(monkeypatch, config, settings):
    store = Store({runtime._snapshot_key("devin-1"): "im-expired"})
    fresh = Mock(name="fresh")
    create = Mock(side_effect=[modal.exception.NotFoundError("gone"), fresh])
    monkeypatch.setattr(runtime, "_new_sandbox", create)
    from_id = Mock(return_value=Mock(name="snapshot_image"))
    monkeypatch.setattr(runtime.modal.Image, "from_id", from_id)

    result = runtime._create_sandbox(
        app=Mock(),
        base_image=Mock(name="base_image"),
        session_id="devin-1",
        snapshot_store=store,
        settings=settings,
        readiness_timeout_seconds=settings.sandbox_ready_timeout_seconds,
        sandbox_options={},
    )

    assert result is fresh
    assert runtime._snapshot_key("devin-1") not in store.values
    assert create.call_count == 2


def test_readiness_failure_terminates_the_half_started_sandbox(monkeypatch, settings):
    sandbox = Mock()
    sandbox.wait_until_ready.side_effect = modal.exception.NotFoundError("image expired")
    create = Mock(return_value=sandbox)
    monkeypatch.setattr(runtime.modal.Sandbox, "create", create)

    with pytest.raises(modal.exception.NotFoundError):
        runtime._new_sandbox(
            app=Mock(),
            image=Mock(),
            settings=settings,
            readiness_timeout_seconds=settings.sandbox_ready_timeout_seconds,
            sandbox_options={},
        )

    sandbox.terminate.assert_called_once_with()
    assert create.call_args.kwargs["timeout"] == runtime._sandbox_lifetime_seconds(settings)


def test_sidecar_readiness_failure_includes_diagnostics():
    process = Mock()
    process.wait.return_value = 1
    process.stdout.read.return_value = "connection refused"
    sandbox = Mock()
    sandbox.exec.return_value = process

    with pytest.raises(RuntimeError, match="connection refused"):
        runtime._wait_for_sidecar(sandbox, timeout_seconds=17)

    assert "seq 1 85" in sandbox.exec.call_args.args[2]


def test_status_lookup_retries_transport_errors_then_recovers(settings):
    client = Client(
        status="suspended",
        status_errors=[OutpostsAPIError("one"), OutpostsAPIError("two")],
    )

    known, status = runtime._final_status(
        cast(OutpostsClient, client),
        session_id="devin-1",
        settings=settings,
    )

    assert known is True
    assert status == "suspended"


def test_status_lookup_retries_successful_but_stale_states(settings):
    client = Client(statuses=["running", "running", "terminated"])

    known, status = runtime._final_status(
        cast(OutpostsClient, client),
        session_id="devin-1",
        settings=settings,
    )

    assert known is True
    assert status == "terminated"
    assert client.statuses == []


def test_claim_deadline_bounds_sandbox_and_sidecar_startup(monkeypatch, config, settings):
    now = 1_000.0
    sandbox = Sandbox()
    create_sandbox = Mock(return_value=sandbox)
    wait_for_sidecar = Mock()
    monkeypatch.setattr(runtime.time, "time", Mock(return_value=now))
    monkeypatch.setattr(runtime, "_create_sandbox", create_sandbox)
    monkeypatch.setattr(runtime, "_create_sidecar", Mock())
    monkeypatch.setattr(runtime, "_wait_for_sidecar", wait_for_sidecar)

    runtime._run_claimed_session(
        app=Mock(),
        image=Mock(),
        config=config,
        settings=settings,
        client=cast(OutpostsClient, Client(status="terminated")),
        snapshot_store=Store(),
        session_id="devin-1",
        acceptor_id="modal-demo-attempt",
        claim_deadline=str(now + 100),
        connect_token="connect-token",
        gateway_url="wss://gateway.example",
        sidecar_image_id="im-sidecar",
        sandbox_options={},
    )

    assert create_sandbox.call_args.kwargs["readiness_timeout_seconds"] == 25
    wait_for_sidecar.assert_called_once_with(
        sandbox,
        timeout_seconds=settings.sidecar_ready_timeout_seconds,
    )


def test_expired_claim_deadline_fails_before_creating_a_sandbox(monkeypatch, config, settings):
    create_sandbox = Mock()
    monkeypatch.setattr(runtime.time, "time", Mock(return_value=1_000.0))
    monkeypatch.setattr(runtime, "_create_sandbox", create_sandbox)

    with pytest.raises(ClaimDeadlineError, match="not enough time"):
        runtime._run_claimed_session(
            app=Mock(),
            image=Mock(),
            config=config,
            settings=settings,
            client=cast(OutpostsClient, Client()),
            snapshot_store=Store(),
            session_id="devin-1",
            acceptor_id="modal-demo-attempt",
            claim_deadline="999",
            connect_token="connect-token",
            gateway_url="wss://gateway.example",
            sidecar_image_id="im-sidecar",
            sandbox_options={},
        )

    create_sandbox.assert_not_called()


def test_snapshot_without_an_image_id_fails_loudly(settings):
    sandbox = Mock()
    sandbox.snapshot_filesystem.return_value = SimpleNamespace(object_id=None)

    with pytest.raises(RuntimeError, match="without an object ID"):
        runtime._snapshot(
            sandbox,
            session_id="devin-1",
            snapshot_store=Store(),
            settings=settings,
        )


def test_missing_cached_sidecar_is_evicted_for_the_next_scheduler(monkeypatch, config, settings):
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-deleted"})
    sandbox = Sandbox()
    monkeypatch.setattr(runtime, "_create_sandbox", Mock(return_value=sandbox))
    monkeypatch.setattr(
        runtime,
        "_create_sidecar",
        Mock(side_effect=modal.exception.NotFoundError("image deleted")),
    )

    with pytest.raises(modal.exception.NotFoundError):
        runtime._run_claimed_session(
            app=Mock(),
            image=Mock(),
            config=config,
            settings=settings,
            client=cast(OutpostsClient, Client()),
            snapshot_store=store,
            session_id="devin-1",
            acceptor_id="modal-demo-attempt",
            claim_deadline=None,
            connect_token="connect-token",
            gateway_url="wss://gateway.example",
            sidecar_image_id="im-deleted",
            sandbox_options={},
        )

    assert runtime._SIDECAR_IMAGE_KEY not in store.values
    assert sandbox.terminated


class PollClient:
    def __init__(self, pending=("devin-1",), claim_error=None):
        self.pending = pending
        self.claim_error = claim_error
        self.claimed = []
        self.released = []

    def pending_sessions(self, outpost_id):
        if isinstance(self.pending, BaseException):
            raise self.pending
        return tuple(
            entry if isinstance(entry, PendingSession) else PendingSession(entry, None)
            for entry in self.pending
        )

    def claim(self, session_id, acceptor_id):
        if self.claim_error:
            raise self.claim_error
        self.claimed.append((session_id, acceptor_id))
        return Claim("tomorrow", "connect-token", "wss://gateway.example")

    def release(self, session_id, acceptor_id):
        self.released.append((session_id, acceptor_id))


def test_scheduler_caches_sidecar_and_dispatches_claim(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    build = Mock(return_value="im-sidecar")
    monkeypatch.setattr(runtime, "_build_sidecar_image_id", build)
    store = Store()
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    trace_context = {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
    monkeypatch.setattr(runtime.observability, "get_context", Mock(return_value=trace_context))
    spawn = Mock()

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=spawn,
        token="token",
    )

    assert store.values[runtime._SIDECAR_IMAGE_KEY] == "im-sidecar"
    lease = store.values[runtime._dispatch_key("devin-1")]
    assert lease[runtime._DISPATCH_TRACE_CONTEXT_KEY] == trace_context
    build.assert_called_once_with(config.image_build_app_name)
    spawn.assert_called_once_with("devin-1")


def test_session_attaches_scheduler_trace_context(monkeypatch, config, settings):
    trace_context = {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
    store = Store(
        {
            runtime._SIDECAR_IMAGE_KEY: "im-sidecar",
            runtime._dispatch_key("devin-1"): {
                runtime._DISPATCH_EXPIRY_KEY: 9_999_999.0,
                runtime._DISPATCH_TRACE_CONTEXT_KEY: trace_context,
            },
        }
    )
    client = Client(claim_error=ClaimConflict("devin-1"))
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    attach_context = Mock(wraps=runtime.observability.attach_context)
    monkeypatch.setattr(runtime.observability, "attach_context", attach_context)

    runtime.execute_session(
        app=Mock(),
        image=Mock(),
        config=config,
        settings=settings,
        session_id="devin-1",
        token="token",
        sandbox_options={},
    )

    attach_context.assert_called_once_with(trace_context)


def test_scheduler_does_not_release_an_unclaimed_session_when_spawn_fails(
    monkeypatch, config, settings
):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    spawn = Mock(side_effect=RuntimeError("no capacity"))

    with pytest.raises(ExceptionGroup, match="session dispatches failed") as exc_info:
        runtime.dispatch_pending_sessions(
            config=config,
            settings=settings,
            spawn_session=spawn,
            token="token",
        )

    assert len(exc_info.value.exceptions) == 1
    assert "devin-1" in "\n".join(exc_info.value.exceptions[0].__notes__)
    assert runtime._dispatch_key("devin-1") not in store.values
    assert client.released == []
    assert client.claimed == []


def test_scheduler_dispatches_without_acquiring_the_claim(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    spawn = Mock()

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=spawn,
        token="token",
    )

    spawn.assert_called_once_with("devin-1")
    assert client.claimed == []


def test_scheduler_request_failure_is_not_hidden(monkeypatch, config, settings):
    client = PollClient(pending=OutpostsAPIError("offline"))
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=Store()))
    spawn = Mock()

    with pytest.raises(OutpostsAPIError, match="offline"):
        runtime.dispatch_pending_sessions(
            config=config,
            settings=settings,
            spawn_session=spawn,
            token="token",
        )

    spawn.assert_not_called()


def test_scheduler_protocol_failure_is_not_hidden(monkeypatch, config, settings):
    client = PollClient(pending=OutpostsProtocolError("invalid response"))
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=Store()))

    with pytest.raises(OutpostsProtocolError, match="invalid response"):
        runtime.dispatch_pending_sessions(
            config=config,
            settings=settings,
            spawn_session=Mock(),
            token="token",
        )


def test_scheduler_does_not_claim_until_sidecar_is_ready(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(
        runtime,
        "_build_sidecar_image_id",
        Mock(side_effect=RuntimeError("build unavailable")),
    )
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=Store()))

    with pytest.raises(RuntimeError, match="build unavailable"):
        runtime.dispatch_pending_sessions(
            config=config,
            settings=settings,
            spawn_session=Mock(),
            token="token",
        )

    assert client.claimed == []


def test_scheduler_attempts_all_dispatches_before_reporting_failures(monkeypatch, config, settings):
    client = PollClient(pending=("devin-1", "devin-2", "devin-3"))
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    spawn = Mock(side_effect=[RuntimeError("one"), None, RuntimeError("three")])

    with pytest.raises(ExceptionGroup) as exc_info:
        runtime.dispatch_pending_sessions(
            config=config,
            settings=settings,
            spawn_session=spawn,
            token="token",
        )

    assert [call.args[0] for call in spawn.call_args_list] == ["devin-1", "devin-2", "devin-3"]
    assert len(exc_info.value.exceptions) == 2


def test_scheduler_deduplicates_queued_session_invocations(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    monkeypatch.setattr(runtime.time, "time", Mock(return_value=1_000.0))
    spawn = Mock()

    for _ in range(2):
        runtime.dispatch_pending_sessions(
            config=config,
            settings=settings,
            spawn_session=spawn,
            token="token",
        )

    spawn.assert_called_once_with("devin-1")
    lease = store.values[runtime._dispatch_key("devin-1")]
    assert lease[runtime._DISPATCH_EXPIRY_KEY] > 1_000.0


def test_scheduler_retries_an_expired_dispatch_lease(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    lease_key = runtime._dispatch_key("devin-1")
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar", lease_key: 999.0})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    monkeypatch.setattr(runtime.time, "time", Mock(return_value=1_000.0))
    spawn = Mock()

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=spawn,
        token="token",
    )

    spawn.assert_called_once_with("devin-1")
    assert store.values[lease_key][runtime._DISPATCH_EXPIRY_KEY] > 1_000.0


def test_scheduler_refreshes_snapshot_index_once_per_day(monkeypatch):
    snapshot_key = runtime._snapshot_key("devin-1")
    store = Store({snapshot_key: "im-snapshot", runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    get = Mock(wraps=store.get)
    store.get = get
    now = 1_000_000.0
    monkeypatch.setattr(runtime.time, "time", Mock(return_value=now))

    runtime._refresh_snapshot_index(store)
    runtime._refresh_snapshot_index(store)

    assert sum(call.args == (snapshot_key,) for call in get.call_args_list) == 1
    assert store.values[runtime._SNAPSHOT_INDEX_REFRESH_KEY] == now


def test_scheduler_skips_pending_entries_that_are_not_awaiting_a_worker(
    monkeypatch, config, settings
):
    client = PollClient(
        pending=(
            PendingSession("devin-asleep", SessionStatus.SUSPENDED),
            PendingSession("devin-done", SessionStatus.TERMINATED),
            PendingSession("devin-live", SessionStatus.RUNNING),
        )
    )
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    spawn = Mock()

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=spawn,
        token="token",
    )

    spawn.assert_called_once_with("devin-live")
    assert runtime._dispatch_key("devin-asleep") not in store.values
    assert runtime._dispatch_key("devin-done") not in store.values


class HangingProcess:
    """A worker process that only ends when its sandbox is terminated."""

    def __init__(self, sandbox):
        self._sandbox = sandbox
        self.stdout = self._lines()

    def _lines(self):
        while not self._sandbox.terminated:
            time.sleep(0.005)
        return
        yield  # pragma: no cover - makes _lines a generator

    def wait(self):
        raise RuntimeError("stream reset by sandbox termination")


class HangingSandbox(Sandbox):
    def exec(self, *args, **kwargs):
        self.exec_calls.append((args, kwargs))
        return HangingProcess(self)


def test_watchdog_stops_a_worker_whose_session_left_the_queue(monkeypatch, config):
    settings = WorkerSettings(
        status_attempts=3,
        status_retry_delay_seconds=0,
        snapshot_ttl_seconds=123,
        status_watchdog_interval_seconds=0.01,
    )
    client = Client(status=None)
    sandbox = HangingSandbox()
    snapshot_key = runtime._snapshot_key("devin-1")
    store = Store({snapshot_key: "im-earlier-snapshot"})

    run_claimed(
        monkeypatch,
        config=config,
        settings=settings,
        client=cast(OutpostsClient, client),
        store=store,
        sandbox=sandbox,
    )

    assert sandbox.terminated
    assert sandbox.snapshot_calls == []
    assert store.values[snapshot_key] == "im-earlier-snapshot"


def test_liveness_watchdog_tolerates_transient_suspended_readings():
    client = Client(
        statuses=[
            SessionStatus.SUSPENDED,
            SessionStatus.SUSPENDED,
            SessionStatus.RUNNING,
            SessionStatus.SUSPENDED,
            SessionStatus.SUSPENDED,
            SessionStatus.SUSPENDED,
        ]
    )
    sandbox = Sandbox()
    ended = threading.Event()

    runtime._watch_session_liveness(
        cast(OutpostsClient, client),
        cast(modal.Sandbox, sandbox),
        session_id="devin-1",
        interval_seconds=0,
        stop=threading.Event(),
        ended=ended,
    )

    assert client.statuses == []
    assert ended.is_set()
    assert sandbox.terminated


def test_liveness_watchdog_retries_transport_failures():
    client = Client(
        status=SessionStatus.TERMINATED,
        status_errors=[OutpostsAPIError("offline")],
    )
    sandbox = Sandbox()
    ended = threading.Event()

    runtime._watch_session_liveness(
        cast(OutpostsClient, client),
        cast(modal.Sandbox, sandbox),
        session_id="devin-1",
        interval_seconds=0,
        stop=threading.Event(),
        ended=ended,
    )

    assert ended.is_set()
    assert sandbox.terminated


def test_worker_env_pins_the_sessions_remote_binary(monkeypatch, config, settings):
    sandbox = Sandbox()

    run_claimed(
        monkeypatch,
        config=config,
        settings=settings,
        client=Client(status="suspended"),
        store=Store(),
        sandbox=sandbox,
        remote_binary_sha="fa58fabc31",
    )

    [(_, kwargs)] = sandbox.exec_calls
    assert kwargs["env"]["DEVIN_WORKER_REMOTE_SHA"] == "fa58fabc31"


def test_unpinned_session_does_not_set_a_remote_binary_sha(monkeypatch, config, settings):
    sandbox = Sandbox()

    run_claimed(
        monkeypatch,
        config=config,
        settings=settings,
        client=Client(status="suspended"),
        store=Store(),
        sandbox=sandbox,
    )

    [(_, kwargs)] = sandbox.exec_calls
    assert "DEVIN_WORKER_REMOTE_SHA" not in kwargs["env"]


def test_execute_session_reads_the_pin_from_the_queue_entry_when_the_claim_lacks_it(
    monkeypatch, config, settings
):
    client = Client(status="suspended", entry_remote_sha="fa58fabc31")
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    run = Mock()
    monkeypatch.setattr(runtime, "_run_claimed_session", run)

    runtime.execute_session(
        app=Mock(),
        image=Mock(),
        config=config,
        settings=settings,
        session_id="devin-1",
        token="token",
        sandbox_options={},
    )

    assert run.call_args.kwargs["remote_binary_sha"] == "fa58fabc31"


def test_execute_session_prefers_the_claims_pin(monkeypatch, config, settings):
    client = Client(status="suspended", claim_remote_sha="aaa111", entry_remote_sha="bbb222")
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    run = Mock()
    monkeypatch.setattr(runtime, "_run_claimed_session", run)

    runtime.execute_session(
        app=Mock(),
        image=Mock(),
        config=config,
        settings=settings,
        session_id="devin-1",
        token="token",
        sandbox_options={},
    )

    assert run.call_args.kwargs["remote_binary_sha"] == "aaa111"
