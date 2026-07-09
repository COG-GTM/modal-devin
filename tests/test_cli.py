from __future__ import annotations

import json
import runpy
import subprocess
from pathlib import Path

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
    assert "POOL_CONFIG = outpost.OutpostPoolConfig" in source
    assert "config=POOL_CONFIG" in source


def test_create_modal_secret_uses_a_temp_json_file_not_argv(monkeypatch):
    seen_paths: list[Path] = []

    def fake_modal(*args: str):
        assert args[0:3] == ("secret", "create", "--from-json")
        assert args[-1] == "devin-outposts-token"
        assert not any("super-secret-token" in arg for arg in args)

        secret_path = Path(args[3])
        seen_paths.append(secret_path)
        assert secret_path.exists()
        assert json.loads(secret_path.read_text()) == {
            "DEVIN_OUTPOSTS_TOKEN": "super-secret-token"
        }
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cli, "_modal", fake_modal)

    result = cli._create_modal_secret("devin-outposts-token", "super-secret-token")

    assert result.returncode == 0
    assert seen_paths
    assert not seen_paths[0].exists()


def test_create_can_deploy_with_modal_devins_python_environment(tmp_path, monkeypatch):
    deployed: list[Path] = []

    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setattr(cli, "_modal_deploy", lambda path: deployed.append(path) or 0)

    cli.create(
        name="demo-pool",
        pool_id="outpost_env-demo",
        pools_dir=str(tmp_path),
        deploy=True,
    )

    assert deployed == [tmp_path / "demo_pool.py"]


def test_create_no_deploy_skips_the_interactive_deploy_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_modal_is_configured", lambda: True)
    monkeypatch.setattr(cli, "_existing_secret_names", lambda: {"devin-outposts-token"})
    monkeypatch.setattr(
        cli,
        "_confirm",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
    )

    cli.create(
        name="demo-pool",
        pool_id="outpost_env-demo",
        pools_dir=str(tmp_path),
        deploy=False,
    )

    assert (tmp_path / "demo_pool.py").exists()


def test_outpost_deploy_wraps_modal_deploy(monkeypatch):
    deployed: list[Path] = []
    monkeypatch.setattr(cli, "_modal_deploy", lambda path: deployed.append(path) or 0)

    cli.deploy(Path("pools/demo.py"))

    assert deployed == [Path("pools/demo.py")]


def test_create_help_uses_explicit_parameter_descriptions(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.app(["outpost", "create", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "Scaffold a Modal pool file for Devin Outposts." in help_text
    assert "Human-readable Devin worker pool name." in help_text
    assert "Scaffold a new pool file under pools/.py" not in help_text
    assert '[default: ""]' not in help_text
