from __future__ import annotations

import runpy

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
