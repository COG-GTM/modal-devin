from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import modal
import pytest

from modal_devin import ModalCompatibilityError
from modal_devin.images import (
    _CADDYFILE,
    _DEVIN_BIN,
    _DEVIN_CLI_CONTRACT_CHECK,
    _DEVIN_CLI_INSTALL,
    _SIDECAR_RECIPE_DIGEST,
    _controller_image,
    _create_sidecar,
    _sidecar_image,
    _worker_image,
)


def test_worker_image_remains_composable():
    image = _worker_image(install_chrome=False, install_ffmpeg=False)

    customized = image.run_commands("echo custom")

    assert isinstance(customized, modal.Image)
    assert "local files" not in repr(image)


def test_devin_installer_does_not_suppress_failures():
    assert "|| true" not in _DEVIN_CLI_INSTALL


def test_devin_installer_verifies_the_binary_instead_of_trusting_install_shs_exit_code():
    # install.sh's own last step runs `devin setup`, an interactive OAuth wizard that
    # always fails without a TTY (as in an image build) even when the binary installed
    # fine. The install command must still fail if the binary is genuinely missing.
    assert _DEVIN_CLI_INSTALL.endswith(f"; test -x {_DEVIN_BIN}")


def test_worker_image_checks_the_unpinned_cli_contract_during_build():
    assert "--session" in _DEVIN_CLI_CONTRACT_CHECK
    assert "--pool" in _DEVIN_CLI_CONTRACT_CHECK
    assert "--acceptor-id" in _DEVIN_CLI_CONTRACT_CHECK
    assert "DEVIN_REMOTE_SESSION_TOKEN" in _DEVIN_CLI_CONTRACT_CHECK


def test_controller_image_is_small_and_contains_the_runtime_source():
    image = _controller_image()

    representation = repr(image)
    assert "local files" in representation
    assert "chromium" not in representation
    assert "ffmpeg" not in representation


def test_sidecar_recipe_has_a_stable_cache_digest():
    assert len(_SIDECAR_RECIPE_DIGEST) == 12
    assert _SIDECAR_RECIPE_DIGEST.isalnum()


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
