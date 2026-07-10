from __future__ import annotations

import json
import runpy
import subprocess
from pathlib import Path

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
    assert "image = worker.prepare_image(base_image)" in source
    assert source.count("@app.function(") == 2
    assert "schedule=modal.Period(" in source
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


def test_create_modal_secret_uses_a_temp_json_file_not_argv(monkeypatch):
    seen_paths: list[Path] = []

    def fake_modal(*args: str):
        assert args[0:3] == ("secret", "create", "--from-json")
        assert not any("super-secret-token" in arg for arg in args)
        secret_path = Path(args[3])
        seen_paths.append(secret_path)
        assert json.loads(secret_path.read_text()) == {"DEVIN_OUTPOSTS_TOKEN": "super-secret-token"}
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cli, "_modal", fake_modal)

    cli._create_modal_secret("devin-outposts-token", "super-secret-token")

    assert seen_paths and not seen_paths[0].exists()


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


def test_secret_listing_validates_modal_json(monkeypatch):
    result = subprocess.CompletedProcess(
        (),
        0,
        json.dumps([{"name": "one"}, {"name": "two"}, {"bad": "item"}]),
        "",
    )
    monkeypatch.setattr(cli, "_modal", lambda *args: result)

    assert cli._existing_secret_names() == {"one", "two"}

    monkeypatch.setattr(
        cli,
        "_modal",
        lambda *args: subprocess.CompletedProcess((), 0, "not-json", ""),
    )
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


def test_cli_help_presents_the_project_level_workflow(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.app(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "init" in output
    assert "deploy" in output
    assert "doctor" in output
    assert "outpost" not in output
