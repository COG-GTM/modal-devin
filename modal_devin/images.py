"""Composable Modal image recipes for Devin workers."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Collection
from typing import Protocol, cast

import modal

from modal_devin._exceptions import ModalCompatibilityError

_DEVIN_BIN = "/root/.local/bin/devin"
# Intentionally left unpinned for now. Supply-chain reproducibility is tracked separately.
# install.sh's own last step unconditionally runs `devin setup`, an interactive OAuth
# wizard that always fails without a TTY (as in this build). Workers authenticate via
# DEVIN_OUTPOSTS_TOKEN instead, so that failure is harmless -- but it's still the exit
# code of the whole `curl | bash` pipeline. Chaining the executable check with `;` makes
# "the binary actually works" the real success signal instead, so a genuine install
# failure (bad download, missing binary, ...) still fails the build.
_DEVIN_CLI_INSTALL = f"curl -fsSL https://cli.devin.ai/install.sh | bash; test -x {_DEVIN_BIN}"

_CADDYFILE = """\
{
	admin off
	auto_https off
}

:{$SIDECAR_PORT} {
	log {
		output stdout
		format json
	}
	handle /_modal_devin/health {
		respond "ok" 200
	}
	handle /opbeta/outposts/* {
		reverse_proxy {$UPSTREAM_URL} {
			header_up Host {upstream_hostport}
			header_up Authorization "Bearer {$DEVIN_OUTPOSTS_TOKEN}"
		}
	}
	handle {
		respond "forbidden" 403
	}
}
"""

# Intentionally left unpinned for now. Supply-chain reproducibility is tracked separately.
_CADDY_INSTALL = (
    "curl -s https://api.github.com/repos/caddyserver/caddy/releases/latest "
    "| grep browser_download_url | grep linux_amd64.tar.gz | cut -d '\"' -f 4 "
    "| xargs curl -fsSL -o /tmp/caddy.tar.gz "
    "&& tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy "
    "&& chmod +x /usr/local/bin/caddy "
    "&& rm /tmp/caddy.tar.gz"
)
# Bump the schema if _sidecar_image changes outside these embedded inputs.
_SIDECAR_RECIPE_SCHEMA = "1"
_SIDECAR_RECIPE_DIGEST = hashlib.sha256(
    (_SIDECAR_RECIPE_SCHEMA + "\0" + _CADDY_INSTALL + "\0" + _CADDYFILE).encode()
).hexdigest()[:12]

_DUMMY_TOKEN = "cog_sidecarmanaged00000000000000000000000000000000"
_SIDECAR_PORT = 8686
_CHROME_PATH = "/usr/bin/chromium"


class _SidecarManager(Protocol):
    def create(
        self,
        *args: str,
        name: str,
        image: modal.Image,
        env: dict[str, str],
        secrets: Collection[modal.Secret],
    ) -> object: ...


def _worker_image(
    *,
    python_version: str = "3.12",
    install_ffmpeg: bool = True,
    install_chrome: bool = True,
) -> modal.Image:
    """Return a composable base image for a Devin worker.

    The modal-devin source mount is deliberately *not* added here. The public
    ``Worker.prepare_image()`` phase adds it after all user build steps, which keeps
    Modal's fluent image API valid during customization.
    """
    image = (
        modal.Image.debian_slim(python_version=python_version)
        .apt_install("git", "curl", "ca-certificates", "tar")
        .run_commands(_DEVIN_CLI_INSTALL)
        .run_commands("mkdir -p /root/workspace")
    )
    if install_ffmpeg:
        image = image.apt_install("ffmpeg")
    if install_chrome:
        image = image.apt_install("chromium").env({"DEVIN_CHROME_PATH": _CHROME_PATH})
    return image


def _controller_image(*, python_version: str = "3.12") -> modal.Image:
    """Return a small image for scheduler and other control-plane functions."""
    return modal.Image.debian_slim(python_version=python_version).add_local_python_source(
        "modal_devin"
    )


def _finalize_worker_image(image: modal.Image) -> modal.Image:
    """Add modal-devin source as the final, startup-mounted image operation."""
    return image.add_local_python_source("modal_devin")


def _sidecar_image() -> modal.Image:
    """Return the Caddy image that keeps the raw Devin token out of the main Sandbox."""
    return (
        modal.Image.debian_slim()
        .apt_install("curl", "ca-certificates", "tar")
        .run_commands(_CADDY_INSTALL)
        .run_commands("test -x /usr/local/bin/caddy")
        .run_commands(
            "mkdir -p /etc/caddy && printf '%b' "
            + shlex.quote(_CADDYFILE.replace("\n", "\\n"))
            + " > /etc/caddy/Caddyfile"
        )
    )


def _build_sidecar_image_id(build_app_name: str) -> str:
    """Build the sidecar image eagerly and return its Modal object ID."""
    build_app = modal.App.lookup(build_app_name, create_if_missing=True)
    image_id = _sidecar_image().build(build_app).object_id
    if image_id is None:
        raise ModalCompatibilityError("Modal returned a sidecar image without an object ID")
    return image_id


def _create_sidecar(
    sandbox: modal.Sandbox,
    *,
    sidecar_image_id: str,
    api_url: str,
    token: str,
) -> None:
    """Attach the experimental token-injecting sidecar behind one compatibility boundary."""
    manager = getattr(sandbox, "_experimental_sidecars", None)
    if manager is None:
        raise ModalCompatibilityError(
            "This modal-devin release requires Modal Sandbox sidecar support; "
            "install a supported Modal SDK version."
        )
    sidecars = cast(_SidecarManager, manager)
    sidecars.create(
        "caddy",
        "run",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
        name="caddy",
        image=modal.Image.from_id(sidecar_image_id),
        env={"SIDECAR_PORT": str(_SIDECAR_PORT), "UPSTREAM_URL": api_url},
        secrets=[modal.Secret.from_dict({"DEVIN_OUTPOSTS_TOKEN": token})],
    )
