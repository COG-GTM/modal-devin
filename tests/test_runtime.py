from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import modal
import pytest

from modal_devin import (
    ModalCompatibilityError,
    OutpostsAPIError,
    SessionStatusUnknownError,
    WorkerExitedError,
)
from modal_devin import (
    _runtime as runtime,
)
from modal_devin._client import Claim, ClaimConflict, OutpostsClient
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

    def snapshot_filesystem(self, *, ttl):
        self.snapshot_calls.append(ttl)
        return SimpleNamespace(object_id="im-snapshot")

    def terminate(self):
        self.terminated = True


class Client:
    token = "real-token"

    def __init__(self, *, status=None, status_errors=()):
        self.status = status
        self.status_errors = list(status_errors)
        self.releases = []

    def session_status(self, session_id, acceptor_id):
        if self.status_errors:
            raise self.status_errors.pop(0)
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


def run_claimed(monkeypatch, *, config, settings, client, store, sandbox):
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
        sidecar_image_id="im-sidecar",
        sandbox_options={},
    )


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

    assert store.values["devin-1"] == "im-snapshot"
    assert sandbox.snapshot_calls == [123]
    assert sandbox.terminated
    [(_args, kwargs)] = sandbox.exec_calls
    assert kwargs["stderr"].name == "STDOUT"


def test_completed_session_removes_old_snapshot(monkeypatch, config, settings):
    store = Store({"devin-1": "im-old"})

    run_claimed(
        monkeypatch,
        config=config,
        settings=settings,
        client=Client(status=None),
        store=store,
        sandbox=Sandbox(),
    )

    assert "devin-1" not in store.values


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

    assert store.values["devin-1"] == "im-snapshot"
    assert sandbox.terminated


@pytest.mark.parametrize("status", ["pending", "claimed", "running", "resuming"])
def test_active_status_after_exit_is_preserved_for_recovery(monkeypatch, config, settings, status):
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

    assert store.values["devin-1"] == "im-snapshot"


def test_run_session_releases_claim_after_any_failure(monkeypatch, config, settings):
    client = Client()
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
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

    assert client.releases == [("devin-1", config.acceptor_id)]


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

    assert client.releases == [("devin-1", config.acceptor_id)]


def test_lazy_snapshot_expiry_falls_back_and_forgets_mapping(monkeypatch, config, settings):
    store = Store({"devin-1": "im-expired"})
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
        sandbox_options={},
    )

    assert result is fresh
    assert "devin-1" not in store.values
    assert create.call_count == 2


def test_readiness_failure_terminates_the_half_started_sandbox(monkeypatch, settings):
    sandbox = Mock()
    sandbox.wait_until_ready.side_effect = modal.exception.NotFoundError("image expired")
    monkeypatch.setattr(runtime.modal.Sandbox, "create", Mock(return_value=sandbox))

    with pytest.raises(modal.exception.NotFoundError):
        runtime._new_sandbox(
            app=Mock(),
            image=Mock(),
            settings=settings,
            sandbox_options={},
        )

    sandbox.terminate.assert_called_once_with()


def test_sidecar_readiness_failure_includes_diagnostics():
    process = Mock()
    process.wait.return_value = 1
    process.stdout.read.return_value = "connection refused"
    sandbox = Mock()
    sandbox.exec.return_value = process

    with pytest.raises(RuntimeError, match="connection refused"):
        runtime._wait_for_sidecar(sandbox)


def test_status_lookup_retries_transport_errors_then_recovers(settings):
    client = Client(
        status="suspended",
        status_errors=[OutpostsAPIError("one"), OutpostsAPIError("two")],
    )

    known, status = runtime._final_status(
        cast(OutpostsClient, client),
        session_id="devin-1",
        acceptor_id="modal-demo",
        settings=settings,
    )

    assert known is True
    assert status == "suspended"


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

    def pending_session_ids(self, pool_id):
        if isinstance(self.pending, BaseException):
            raise self.pending
        return self.pending

    def claim(self, session_id, acceptor_id):
        if self.claim_error:
            raise self.claim_error
        self.claimed.append((session_id, acceptor_id))
        return Claim("tomorrow")

    def release(self, session_id, acceptor_id):
        self.released.append((session_id, acceptor_id))


def test_scheduler_caches_sidecar_and_dispatches_claim(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    build = Mock(return_value="im-sidecar")
    monkeypatch.setattr(runtime, "_build_sidecar_image_id", build)
    store = Store()
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    spawn = Mock()

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=spawn,
        token="token",
    )

    assert store.values[runtime._SIDECAR_IMAGE_KEY] == "im-sidecar"
    build.assert_called_once_with(config.image_build_app_name)
    spawn.assert_called_once_with("devin-1")


def test_scheduler_releases_claim_when_spawn_fails(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    store = Store({runtime._SIDECAR_IMAGE_KEY: "im-sidecar"})
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=store))
    spawn = Mock(side_effect=RuntimeError("no capacity"))

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=spawn,
        token="token",
    )

    assert client.released == [("devin-1", config.acceptor_id)]


def test_scheduler_treats_claim_conflict_as_normal_contention(monkeypatch, config, settings):
    client = PollClient(claim_error=ClaimConflict("devin-1"))
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

    spawn.assert_not_called()


def test_scheduler_request_failure_is_deferred_to_the_next_tick(monkeypatch, config, settings):
    client = PollClient(pending=OutpostsAPIError("offline"))
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=Store()))
    spawn = Mock()

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=spawn,
        token="token",
    )

    spawn.assert_not_called()


def test_scheduler_does_not_claim_until_sidecar_is_ready(monkeypatch, config, settings):
    client = PollClient()
    monkeypatch.setattr(runtime, "OutpostsClient", Mock(return_value=client))
    monkeypatch.setattr(
        runtime,
        "_build_sidecar_image_id",
        Mock(side_effect=RuntimeError("build unavailable")),
    )
    monkeypatch.setattr(runtime.modal.Dict, "from_name", Mock(return_value=Store()))

    runtime.dispatch_pending_sessions(
        config=config,
        settings=settings,
        spawn_session=Mock(),
        token="token",
    )

    assert client.claimed == []
