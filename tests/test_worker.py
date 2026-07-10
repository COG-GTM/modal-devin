from __future__ import annotations

import importlib.metadata
import inspect
from dataclasses import FrozenInstanceError
from typing import Any, cast
from unittest.mock import Mock

import modal
import pytest

import modal_devin
from modal_devin import ConfigurationError, Worker, WorkerSettings
from modal_devin import worker as worker_module


def test_worker_is_an_immutable_configured_runtime():
    worker = Worker("Demo / Worker", pool_id="outpost_env-demo")

    assert worker.name == "Demo / Worker"
    assert worker.app_name.startswith("modal-devin-demo-worker-")
    with pytest.raises(FrozenInstanceError):
        worker.settings = WorkerSettings()  # ty: ignore[invalid-assignment]


def test_worker_exposes_domain_operations_not_an_application_builder():
    public_methods = {
        name
        for name, member in inspect.getmembers(Worker, predicate=inspect.isfunction)
        if not name.startswith("_")
    }

    assert public_methods == {
        "base_image",
        "controller_image",
        "dispatch_pending_sessions",
        "prepare_image",
        "run_session",
    }
    assert not hasattr(Worker, "app")


def test_base_image_remains_composable_until_prepare_image():
    worker = Worker("demo", pool_id="pool")
    base_image = worker.base_image(install_chrome=False, install_ffmpeg=False)
    customized = base_image.run_commands("echo custom")

    image = worker.prepare_image(customized)

    assert isinstance(image, modal.Image)
    assert image is not customized
    assert "local files" in repr(image)


def test_controller_image_does_not_carry_worker_dependencies():
    worker = Worker("demo", pool_id="pool")

    image = worker.controller_image()

    assert "local files" in repr(image)
    assert "chromium" not in repr(image)


def test_session_function_timeout_includes_startup_and_cleanup_margin():
    worker = Worker(
        "demo",
        pool_id="pool",
        settings=WorkerSettings(
            session_timeout_seconds=900,
            sandbox_ready_timeout_seconds=45,
            sidecar_ready_timeout_seconds=20,
            snapshot_timeout_seconds=30,
            api_timeout_seconds=10,
            status_attempts=3,
            status_retry_delay_seconds=2,
        ),
    )

    assert worker.session_function_timeout_seconds == 1081


def test_run_session_delegates_runtime_mechanics(monkeypatch):
    worker = Worker("demo", pool_id="pool")
    app = modal.App("demo")
    image = modal.Image.debian_slim()
    execute = Mock()
    monkeypatch.setattr(worker_module, "_execute_session", execute)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "real-token")

    worker.run_session(
        "devin-1",
        app=app,
        image=image,
        memory=2048,
    )

    assert execute.call_args.kwargs["app"] is app
    assert execute.call_args.kwargs["image"] is image
    assert execute.call_args.kwargs["session_id"] == "devin-1"
    assert execute.call_args.kwargs["token"] == "real-token"
    assert execute.call_args.kwargs["sandbox_options"] == {"memory": 2048}


def test_dispatch_pending_sessions_accepts_a_plain_spawn_callable(monkeypatch):
    worker = Worker("demo", pool_id="pool")
    spawn = Mock()
    schedule = Mock()
    monkeypatch.setattr(worker_module, "_dispatch_pending_sessions", schedule)
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "real-token")

    worker.dispatch_pending_sessions(spawn)

    assert schedule.call_args.kwargs["spawn_session"] is spawn
    assert schedule.call_args.kwargs["token"] == "real-token"


def test_run_session_inherits_the_modal_sandbox_signature():
    worker_parameters = inspect.signature(Worker.run_session).parameters
    sandbox_parameters = inspect.signature(modal.Sandbox.create).parameters

    assert tuple(worker_parameters)[:2] == ("self", "session_id")
    for name, parameter in sandbox_parameters.items():
        assert worker_parameters[name] == parameter


@pytest.mark.parametrize("reserved", ["readiness_probe", "timeout", "workdir"])
def test_run_session_rejects_sandbox_options_that_break_invariants(monkeypatch, reserved):
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "real-token")

    with pytest.raises(ConfigurationError, match="runtime invariants"):
        cast(Any, Worker("demo", pool_id="pool").run_session)(
            "devin-1",
            app=modal.App("demo"),
            image=modal.Image.debian_slim(),
            **{reserved: object()},
        )


def test_run_session_rejects_modal_command_arguments():
    with pytest.raises(TypeError, match="owns the Sandbox command"):
        Worker("demo", pool_id="pool").run_session(
            "devin-1",
            "bash",
            app=modal.App("demo"),
            image=modal.Image.debian_slim(),
        )


def test_worker_reports_a_missing_function_secret_before_runtime_work(monkeypatch):
    monkeypatch.delenv("DEVIN_OUTPOSTS_TOKEN", raising=False)

    with pytest.raises(ConfigurationError, match="attach the Devin token Modal Secret"):
        Worker("demo", pool_id="pool").dispatch_pending_sessions(Mock())


def test_worker_validates_public_runtime_objects():
    with pytest.raises(TypeError, match="settings must"):
        Worker("demo", pool_id="pool", settings=cast(Any, {}))
    with pytest.raises(TypeError, match="image must"):
        Worker("demo", pool_id="pool").prepare_image(cast(Any, object()))
    with pytest.raises(TypeError, match="requires app="):
        Worker("demo", pool_id="pool").run_session(
            "devin-1",
            app=cast(Any, object()),
            image=modal.Image.debian_slim(),
        )
    with pytest.raises(TypeError, match="requires image="):
        Worker("demo", pool_id="pool").run_session(
            "devin-1",
            app=modal.App("demo"),
            image=cast(Any, object()),
        )
    with pytest.raises(TypeError, match="spawn_session must be callable"):
        Worker("demo", pool_id="pool").dispatch_pending_sessions(cast(Any, object()))


def test_public_api_does_not_expose_runtime_wiring():
    assert "Worker" in modal_devin.__all__
    assert "dispatch_pending_sessions" not in modal_devin.__all__
    assert "execute_session" not in modal_devin.__all__
    assert "build_sidecar_image_id" not in modal_devin.__all__


def test_version_has_one_metadata_source():
    assert modal_devin.__version__ == importlib.metadata.version("modal-devin")
