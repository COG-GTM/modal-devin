from __future__ import annotations

import runpy

import pytest

from modal_devin import cli, outpost


def test_create_emits_valid_python_for_awkward_string_values(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: set())

    cli.create(
        name='bad"name\nx',
        pool_id='outpost_env-"quoted"',
        api_url='https://api.example.com/path?x="y"',
        secret_name='sec"ret',
        pools_dir=str(tmp_path),
    )

    generated = tmp_path / "bad_name_x.py"
    source = generated.read_text()
    compile(source, str(generated), "exec")

    assert "POOL_NAME = 'bad\"name\\nx'" in source
    assert "POOL_ID = 'outpost_env-\"quoted\"'" in source
    assert "secret = modal.Secret.from_name('sec\"ret')" in source


def test_generated_pool_import_does_not_build_the_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setattr(
        outpost,
        "build_sidecar_image_id",
        lambda pool_name: (_ for _ in ()).throw(AssertionError("sidecar build at import")),
    )

    cli.create(
        name="demo-pool",
        pool_id="outpost_env-demo",
        pools_dir=str(tmp_path),
    )

    runpy.run_path(str(tmp_path / "demo_pool.py"))


def test_generated_pool_reads_worker_settings_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECS", "17")
    monkeypatch.setenv("WORKER_SESSION_TIMEOUT_SECS", "900")

    cli.create(
        name="demo-pool",
        pool_id="outpost_env-demo",
        pools_dir=str(tmp_path),
    )

    generated = tmp_path / "demo_pool.py"
    source = generated.read_text()
    namespace = runpy.run_path(str(generated))

    assert "schedule=modal.Period(seconds=settings.poll_interval_secs)" in source
    assert "session_timeout_secs=settings.session_timeout_secs" in source
    settings = namespace["settings"]
    assert settings.poll_interval_secs == 17
    assert settings.session_timeout_secs == 900


def test_create_help_uses_explicit_parameter_descriptions(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.app(["outpost", "create", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "Scaffold a Modal pool file for Devin Outposts." in help_text
    assert "Human-readable Devin worker pool name." in help_text
    assert "Scaffold a new pool file under pools/.py" not in help_text
