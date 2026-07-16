from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modal_devin import cli


def initialize(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    cli.init_worker(
        name="demo-pool",
        pool_id="outpost_env-demo",
        pools_dir=tmp_path,
        **kwargs,
    )
    return tmp_path / "demo_pool.py"


def test_init_generates_an_owned_editable_modal_app(tmp_path, monkeypatch):
    generated = initialize(tmp_path, monkeypatch)

    source = generated.read_text()
    namespace = runpy.run_path(str(generated))

    assert len(source.splitlines()) <= 60
    assert "Generated once by modal-devin" in source
    assert "import modal" in source
    assert "from modal_devin import Worker" in source
    assert "worker = Worker.from_env(" in source
    assert "app = modal.App(" in source
    assert "devin_secret = modal.Secret.from_name(" in source
    assert "controller_image = worker.controller_image()" in source
    assert "image = worker.prepare_image(base_image)" in source
    assert source.count("@app.function(") == 2
    assert "schedule=modal.Period(" in source
    assert "max_containers=1" in source
    assert 'name="scheduler",\n    image=controller_image,' in source
    assert "worker.run_session(" in source
    assert "worker.dispatch_pending_sessions(session.spawn)" in source
    assert "worker.app(" not in source
    assert set(namespace["app"].registered_functions) == {"scheduler", "session"}


def test_generated_app_accepts_awkward_human_name_but_uses_safe_resources(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"sec]ret"})

    cli.init_worker(
        name='bad/name "✨"',
        pool_id='outpost_env-"quoted"',
        api_url="https://api.example.com",
        secret_name="sec]ret",
        pools_dir=tmp_path,
    )

    generated = tmp_path / "bad_name.py"
    namespace = runpy.run_path(str(generated))
    worker = namespace["worker"]
    assert "/" not in worker.app_name
    assert namespace["app"].name == worker.app_name
    assert len(namespace["app"].name) < 64


def test_generated_app_reads_typed_worker_settings_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MODAL_DEVIN_SCHEDULER_INTERVAL_SECONDS", "17")
    monkeypatch.setenv("MODAL_DEVIN_SESSION_TIMEOUT_SECONDS", "900")
    generated = initialize(tmp_path, monkeypatch)

    worker = runpy.run_path(str(generated))["worker"]

    assert worker.settings.scheduler_interval_seconds == 17
    assert worker.settings.session_timeout_seconds == 900


def test_generated_app_does_not_build_sidecar_at_import(tmp_path, monkeypatch):
    from modal_devin import _runtime

    monkeypatch.setattr(
        _runtime,
        "_build_sidecar_image_id",
        lambda *_: (_ for _ in ()).throw(AssertionError("build at import")),
    )

    generated = initialize(tmp_path, monkeypatch)

    runpy.run_path(str(generated))


def test_create_modal_secret_uses_the_sdk_without_a_subprocess(monkeypatch):
    create = Mock()
    monkeypatch.setattr(
        cli.modal,
        "Secret",
        SimpleNamespace(objects=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(
        cli,
        "_modal",
        lambda *args: (_ for _ in ()).throw(AssertionError("secret subprocess")),
    )

    cli._create_modal_secret("devin-outposts-token", "super-secret-token")

    create.assert_called_once_with(
        "devin-outposts-token",
        {"DEVIN_OUTPOSTS_TOKEN": "super-secret-token"},
    )


def test_init_can_deploy_from_the_project_environment(tmp_path, monkeypatch):
    deployed = []
    monkeypatch.setattr(cli, "_modal_deploy", lambda path: deployed.append(path) or 0)

    generated = initialize(tmp_path, monkeypatch, deploy=True)

    assert deployed == [generated]


def test_deploy_propagates_modal_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "_modal_deploy", lambda path: 23)

    with pytest.raises(SystemExit) as exc_info:
        cli.deploy(Path("worker.py"))

    assert exc_info.value.code == 23


def test_doctor_reports_failed_required_check(monkeypatch):
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/devin")

    with pytest.raises(SystemExit) as exc_info:
        cli.doctor()

    assert exc_info.value.code == 1


def test_doctor_success_path(monkeypatch):
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/devin")

    cli.doctor()


def test_secret_listing_uses_the_sdk(monkeypatch):
    list_secrets = Mock(
        return_value=[
            SimpleNamespace(name="one"),
            SimpleNamespace(name="two"),
            SimpleNamespace(name=None),
        ]
    )
    monkeypatch.setattr(
        cli.modal,
        "Secret",
        SimpleNamespace(objects=SimpleNamespace(list=list_secrets)),
    )

    assert cli._existing_secret_names() == {"one", "two"}
    list_secrets.assert_called_once_with()

    list_secrets.side_effect = RuntimeError("offline")
    assert cli._existing_secret_names() is None


def test_init_rejects_invalid_api_url_before_remote_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)

    def run(*args, **kwargs):
        raise AssertionError("remote command")

    monkeypatch.setattr(cli.subprocess, "run", run)

    with pytest.raises(SystemExit, match="api_url"):
        cli.init_worker(
            name="demo",
            api_url="file:///tmp/token",
            pools_dir=tmp_path,
        )


def test_init_rejects_empty_secret_name_before_remote_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)

    def run(*args, **kwargs):
        raise AssertionError("remote command")

    monkeypatch.setattr(cli.subprocess, "run", run)

    with pytest.raises(SystemExit, match="secret_name"):
        cli.init_worker(
            name="demo",
            pool_id="outpost_env-demo",
            secret_name=" ",
            pools_dir=tmp_path,
        )


def test_init_refuses_to_overwrite_before_remote_changes(tmp_path, monkeypatch):
    (tmp_path / "demo.py").write_text("existing")
    monkeypatch.setattr(cli, "_interactive", lambda: False)

    def run(*args, **kwargs):
        raise AssertionError("remote command")

    monkeypatch.setattr(cli.subprocess, "run", run)

    with pytest.raises(SystemExit, match="already exists"):
        cli.init_worker(name="demo", pools_dir=tmp_path)


def test_init_reports_devin_pool_creation_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("devin", 120)),
    )

    with pytest.raises(SystemExit, match="120 seconds"):
        cli.init_worker(name="demo", pools_dir=tmp_path)


def test_pool_creation_keeps_the_token_out_of_process_arguments(tmp_path, monkeypatch):
    token = "super-secret-token"
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", token)

    def run(args, **kwargs):
        assert args == [
            "devin",
            "worker",
            "pool",
            "create",
            "demo",
            "--api-url",
            "https://api.beta.devinenterprise.com",
        ]
        assert all(token not in arg for arg in args)
        assert kwargs["env"]["DEVIN_OUTPOSTS_TOKEN"] == token
        return subprocess.CompletedProcess(args, 0, "outpost_env-demo\n", "")

    monkeypatch.setattr(cli.subprocess, "run", run)

    cli.init_worker(name="demo", pools_dir=tmp_path)


def test_cli_help_presents_the_project_level_workflow(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.app(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "init" in output
    assert "deploy" in output
    assert "doctor" in output
    assert "outpost" not in output
