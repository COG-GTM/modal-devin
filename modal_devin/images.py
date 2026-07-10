"""Composable Modal image recipes for Devin workers."""

from __future__ import annotations

import re
import shlex
import urllib.parse
from collections.abc import Collection
from typing import Protocol, cast

import modal

from modal_devin._exceptions import ModalCompatibilityError

# Intentionally left unpinned for now. Supply-chain reproducibility is tracked separately.
_DEVIN_CLI_INSTALL = "curl -fsSL https://cli.devin.ai/install.sh | bash || true"
_DEVIN_BIN = "/root/.local/bin/devin"

_CADDYFILE = """\
{
	admin off
	auto_https off
}

:{$SIDECAR_PORT} {
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

_DUMMY_TOKEN = "cog_sidecarmanaged00000000000000000000000000000000"
_SIDECAR_PORT = 8686
_CHROME_PATH = "/usr/bin/chromium"
_SHELL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        .run_commands(f"test -x {_DEVIN_BIN}")
        .run_commands("mkdir -p /root/workspace")
    )
    if install_ffmpeg:
        image = image.apt_install("ffmpeg")
    if install_chrome:
        image = image.apt_install("chromium").env({"DEVIN_CHROME_PATH": _CHROME_PATH})
    return image


def _finalize_worker_image(image: modal.Image) -> modal.Image:
    """Add modal-devin source as the final, startup-mounted image operation."""
    return image.add_local_python_source("modal_devin")


def clone_private_repo(
    image: modal.Image,
    repo_url: str,
    dest: str,
    *,
    token_secret: modal.Secret,
    token_env_var: str = "GIT_CLONE_TOKEN",
) -> modal.Image:
    """Clone a private repository without persisting its token in the image."""
    if not _SHELL_IDENTIFIER.fullmatch(token_env_var):
        raise ValueError(f"token_env_var must be a valid shell identifier, got: {token_env_var!r}")
    parsed = urllib.parse.urlsplit(repo_url)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path:
        raise ValueError(f"repo_url must be a full https:// URL, got: {repo_url!r}")
    if parsed.username or parsed.password:
        raise ValueError("repo_url must not include credentials")
    if parsed.fragment:
        raise ValueError("repo_url must not include a URL fragment")

    plain_repo_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )
    secret_desc = token_secret.name or "<secret>"
    fail_msg = (
        f"{token_env_var} is empty -- does secret {secret_desc} have a key named {token_env_var}?"
    )
    askpass_script = f"""\
#!/bin/sh
case "$1" in
    *Username*) printf '%s\\n' 'x-access-token' ;;
    *) printf '%s\\n' "${{{token_env_var}}}" ;;
esac
"""
    return image.run_commands(
        "\n".join(
            [
                "set -eu",
                f'test -n "${{{token_env_var}:-}}" '
                f"|| {{ echo {shlex.quote(fail_msg)} >&2; exit 1; }}",
                "askpass=$(mktemp /tmp/modal-devin-askpass.XXXXXX)",
                "trap 'rm -f \"$askpass\"' EXIT",
                f"cat > \"$askpass\" <<'MODAL_DEVIN_ASKPASS'\n{askpass_script}MODAL_DEVIN_ASKPASS",
                'chmod 700 "$askpass"',
                "GIT_TERMINAL_PROMPT=0 "
                'GIT_ASKPASS="$askpass" '
                f"git clone {shlex.quote(plain_repo_url)} {shlex.quote(dest)}",
                f"git -C {shlex.quote(dest)} remote set-url origin {shlex.quote(plain_repo_url)}",
            ]
        ),
        secrets=[token_secret],
    )


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


__all__ = ["clone_private_repo"]
