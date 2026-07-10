from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import modal
import pytest

from modal_devin import ModalCompatibilityError
from modal_devin.images import (
    _CADDYFILE,
    _create_sidecar,
    _sidecar_image,
    _worker_image,
    clone_private_repo,
)


def secret(name="github-token"):
    return cast(modal.Secret, SimpleNamespace(name=name))


def test_worker_image_remains_composable():
    image = _worker_image(install_chrome=False, install_ffmpeg=False)

    customized = image.run_commands("echo custom")

    assert isinstance(customized, modal.Image)
    assert "local files" not in repr(image)


@pytest.mark.parametrize("install_chrome", [True, False])
@pytest.mark.parametrize("install_ffmpeg", [True, False])
def test_worker_image_supports_optional_tooling(install_chrome, install_ffmpeg):
    assert isinstance(
        _worker_image(install_chrome=install_chrome, install_ffmpeg=install_ffmpeg),
        modal.Image,
    )


def test_sidecar_health_check_does_not_proxy_to_devin():
    assert "handle /_modal_devin/health" in _CADDYFILE
    assert 'respond "ok" 200' in _CADDYFILE
    assert "handle /opbeta/outposts/*" in _CADDYFILE
    assert 'respond "forbidden" 403' in _CADDYFILE
    assert isinstance(_sidecar_image(), modal.Image)


@pytest.mark.parametrize(
    "url",
    [
        "github.com/org/repo",
        "http://github.com/org/repo",
        "file:///tmp/repo",
        "https://user:token@github.com/org/repo",
        "https://github.com/org/repo#main",
    ],
)
def test_private_clone_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        clone_private_repo(Mock(), url, "/root/workspace/repo", token_secret=secret())


def test_private_clone_keeps_token_out_of_command_and_remote_url():
    image = Mock()

    clone_private_repo(
        image,
        "https://github.com/acme/widgets",
        "/root/workspace/widgets",
        token_secret=secret(),
    )

    [command] = image.run_commands.call_args.args
    assert "GIT_ASKPASS" in command
    assert "github-token" in command  # secret name is diagnostic context
    assert "GIT_CLONE_TOKEN}" in command
    assert "https://github.com/acme/widgets" in command
    assert image.run_commands.call_args.kwargs["secrets"] == [secret()]


def test_private_clone_rejects_invalid_secret_environment_name():
    with pytest.raises(ValueError, match="shell identifier"):
        clone_private_repo(
            Mock(),
            "https://github.com/acme/widgets",
            "/root/workspace/widgets",
            token_secret=secret(),
            token_env_var="BAD-NAME",
        )


def test_sidecar_adapter_has_a_clear_compatibility_error():
    with pytest.raises(ModalCompatibilityError, match="sidecar support"):
        _create_sidecar(
            cast(modal.Sandbox, SimpleNamespace()),
            sidecar_image_id="im-sidecar",
            api_url="https://api.example.com",
            token="token",
        )


def test_sidecar_adapter_passes_only_the_real_token_to_the_sidecar(monkeypatch):
    manager = Mock()
    sandbox = cast(modal.Sandbox, SimpleNamespace(_experimental_sidecars=manager))
    sidecar = Mock(name="sidecar-image")
    monkeypatch.setattr(modal.Image, "from_id", Mock(return_value=sidecar))
    secret_from_dict = Mock(return_value=Mock(name="secret"))
    monkeypatch.setattr(modal.Secret, "from_dict", secret_from_dict)

    _create_sidecar(
        sandbox,
        sidecar_image_id="im-sidecar",
        api_url="https://api.example.com",
        token="real-token",
    )

    secret_from_dict.assert_called_once_with({"DEVIN_OUTPOSTS_TOKEN": "real-token"})
    assert manager.create.call_args.kwargs["image"] is sidecar
