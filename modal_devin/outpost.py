"""Devin Outposts pool: shared poll/claim/dispatch/sidecar logic.

Mounted into pool images via `Image.add_local_python_source("modal_devin")` -- not pip-installed
at container runtime, just source-mounted so it ships with whatever's on disk at deploy time. Fix
a bug here once, redeploy every pool.

Modal requires `@app.function`-decorated functions to live at module (global) scope in the file
being deployed -- see the pool template. So this module doesn't own the decorated functions
themselves; it owns everything else (the sidecar image, the HTTP client, the actual run/poll
bodies) and the pool file's `run_session`/`poll_and_dispatch` are thin wrappers that call in here.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Collection, Iterable, Mapping
from typing import Protocol, cast

import modal

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

DEVIN_CLI_INSTALL = "curl -fsSL https://cli.devin.ai/install.sh | bash || true"
DEVIN_BIN = "/root/.local/bin/devin"

CADDYFILE = """\
{
	admin off
	auto_https off
}

:{$SIDECAR_PORT} {
	reverse_proxy {$UPSTREAM_URL} {
		header_up Host {upstream_hostport}
		header_up Authorization "Bearer {$DEVIN_OUTPOSTS_TOKEN}"
	}
}
"""

CADDY_INSTALL = (
    "curl -s https://api.github.com/repos/caddyserver/caddy/releases/latest "
    "| grep browser_download_url | grep linux_amd64.tar.gz | cut -d '\"' -f 4 "
    "| xargs curl -fsSL -o /tmp/caddy.tar.gz "
    "&& tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy "
    "&& chmod +x /usr/local/bin/caddy "
    "&& rm /tmp/caddy.tar.gz"
)

DUMMY_TOKEN = "cog_sidecarmanaged00000000000000000000000000000000"
SIDECAR_PORT = 8686
POLL_INTERVAL_SECS = 30
SESSION_TIMEOUT_SECS = 1800
API_TIMEOUT_SECS = 30

CHROME_PATH = "/usr/bin/chromium"
_SHELL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SessionRunner(Protocol):
    """A Modal function-like object that can spawn a session runner."""

    def spawn(self, session_id: str, *, sidecar_image_id: str) -> object: ...


class _SidecarManager(Protocol):
    """The narrow subset of Modal's experimental sidecar manager we rely on."""

    def create(
        self,
        *args: str,
        name: str,
        image: modal.Image,
        env: dict[str, str],
        secrets: Collection[modal.Secret],
    ) -> object: ...


def worker_image(
    *,
    python_version: str = "3.12",
    install_ffmpeg: bool = True,
    install_chrome: bool = True,
) -> modal.Image:
    """Base image for a Devin Outposts worker.

    git is required by Devin itself; installed unconditionally. ffmpeg (screen recording) and
    chromium (browser/computer-use tools, via DEVIN_CHROME_PATH) are Devin's own optional
    dependencies -- on by default so a pool "just works", since Modal caches image layers: only
    the first build after a change to this function pays for them, every later deploy is a cache
    hit. Pass install_ffmpeg=False / install_chrome=False for a leaner image if a pool needs
    neither. (Passwordless sudo, Devin's other optional dependency, is a non-issue here -- the
    Sandbox container already runs as root.)
    """
    image = (
        modal.Image.debian_slim(python_version=python_version)
        .apt_install("git", "curl", "ca-certificates", "tar")
        .run_commands(DEVIN_CLI_INSTALL)
        .run_commands(f"test -x {DEVIN_BIN}")
        .run_commands("mkdir -p /root/workspace")
    )
    if install_ffmpeg:
        image = image.apt_install("ffmpeg")
    if install_chrome:
        image = image.apt_install("chromium").env({"DEVIN_CHROME_PATH": CHROME_PATH})
    return image.add_local_python_source("modal_devin")


def clone_private_repo(
    image: modal.Image,
    repo_url: str,
    dest: str,
    *,
    token_secret: modal.Secret,
    token_env_var: str = "GIT_CLONE_TOKEN",
) -> modal.Image:
    """Clone a private repo into `dest` using a token from `token_secret`.

    DO NOT clone with the token embedded in the URL. Git stores the clone URL in
    `dest/.git/config`, and many failures echo the URL to logs. This uses a temporary
    `GIT_ASKPASS` helper, so the token stays in Modal's build-scoped secret environment rather
    than command argv, logs, or the final remote URL. The helper is deleted before the layer
    commits, and the remote URL is explicitly reset to the credential-free `repo_url`.

    `repo_url` should be the plain https URL (no embedded credentials), e.g.
    "https://github.com/your-org/app". Assumes a GitHub-style PAT or App installation token
    (the "x-access-token" username convention) -- adjust if cloning from a host that expects a
    different credential format.
    """
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
                'trap \'rm -f "$askpass"\' EXIT',
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
    """The Caddy sidecar image: holds the real token, injects it into requests to the Devin API."""
    return (
        modal.Image.debian_slim()
        .apt_install("curl", "ca-certificates", "tar")
        .run_commands(CADDY_INSTALL)
        .run_commands("test -x /usr/local/bin/caddy")
        .run_commands(
            "mkdir -p /etc/caddy && printf '%b' "
            + shlex.quote(CADDYFILE.replace("\n", "\\n"))
            + " > /etc/caddy/Caddyfile"
        )
    )


def build_sidecar_image_id(pool_name: str) -> str:
    """Eagerly build the sidecar image and return its id, ready to pass to a Sandbox.

    Sidecars only accept pre-built images (an id, a name, or a snapshot), so this builds against
    a separate, already-hydrated App rather than the caller's app object, which isn't hydrated yet
    when pool files call this at module scope.
    """
    build_app = modal.App.lookup(f"outpost-pool-{pool_name}-image-builds", create_if_missing=True)
    image_id = _sidecar_image().build(build_app).object_id
    if image_id is None:
        raise RuntimeError("Modal returned a sidecar image without an object id")
    return image_id


def _json_object_from_response(body: bytes, *, url: str) -> JsonObject:
    if not body:
        return {}
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object from {url}, got {type(parsed).__name__}")
    return cast(JsonObject, parsed)


def _api_request(
    api_url: str,
    token: str,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None = None,
    *,
    timeout: float = API_TIMEOUT_SECS,
) -> JsonObject:
    data = json.dumps(body).encode() if body is not None else None
    url = f"{api_url.rstrip('/')}/{path.lstrip('/')}"
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json_object_from_response(resp.read(), url=url)


def _devin_path(session_id: str, action: str) -> str:
    return f"/opbeta/outposts/devins/{urllib.parse.quote(session_id, safe='')}/{action}"


def _query_path(path: str, query: Mapping[str, str]) -> str:
    return f"{path}?{urllib.parse.urlencode(query)}"


def _release(api_url: str, token: str, session_id: str, acceptor_id: str) -> None:
    _api_request(
        api_url,
        token,
        "POST",
        _devin_path(session_id, "release"),
        {"acceptor_id": acceptor_id},
    )


def _release_safely(
    api_url: str,
    token: str,
    session_id: str,
    acceptor_id: str,
    reason: str,
) -> None:
    try:
        _release(api_url, token, session_id, acceptor_id)
    except (urllib.error.URLError, ValueError) as e:
        print(f"[{session_id}] failed to release claim after {reason}: {e}", file=sys.stderr)
    else:
        print(f"[{session_id}] released claim after {reason}")


def _response_items(response: Mapping[str, JsonValue]) -> Iterable[Mapping[str, JsonValue]]:
    items = response.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict):
            yield item


def _nested_string(
    mapping: Mapping[str, JsonValue],
    outer_key: str,
    inner_key: str,
) -> str | None:
    outer = mapping.get(outer_key)
    if not isinstance(outer, dict):
        return None
    value = outer.get(inner_key)
    return value if isinstance(value, str) else None


def _session_id_from_item(item: Mapping[str, JsonValue]) -> str | None:
    return _nested_string(item, "metadata", "session_id")


def _claim_deadline_from_response(response: Mapping[str, JsonValue]) -> str | None:
    return _nested_string(response, "status", "claim_deadline")


def _session_status(api_url: str, token: str, session_id: str, acceptor_id: str) -> str | None:
    """The Outposts API's status.session_status for this session (pending/running/suspended/
    terminated), or None if it's not (or no longer) listed as claimed by us."""
    try:
        claimed = _api_request(
            api_url,
            token,
            "GET",
            _query_path(
                "/opbeta/outposts/devins",
                {"phase": "claimed", "acceptor_id": acceptor_id},
            ),
        )
    except (urllib.error.URLError, ValueError):
        return None
    for item in _response_items(claimed):
        if _session_id_from_item(item) == session_id:
            return _nested_string(item, "status", "session_status")
    return None


def _snapshot_name(pool_name: str, session_id: str) -> str:
    """The published-Image name a session's filesystem snapshot lives under -- the name itself
    *is* the session_id -> snapshot lookup, so there's no separate table to keep in sync."""
    return f"outpost-{pool_name}-session-{session_id}-snapshot"


def _create_sandbox(
    app: modal.App, image: modal.Image, resume_name: str, workdir: str, timeout: int
) -> modal.Sandbox:
    """Start from a published snapshot for `resume_name` if one exists.

    A missing snapshot falls back to the base image. Other Modal errors are allowed to propagate so
    a transient auth/network/quota problem does not silently discard suspended session state.
    (`Image.from_name` is a lazy reference -- nothing about a missing/expired snapshot surfaces
    until Sandbox.create actually resolves it)."""
    try:
        sb = modal.Sandbox.create(
            "sleep",
            "infinity",
            app=app,
            image=modal.Image.from_name(resume_name),
            workdir=workdir,
            timeout=timeout,
        )
        print(f"resuming from snapshot {resume_name!r}")
        return sb
    except modal.exception.NotFoundError:
        pass  # no snapshot for this session yet -- the common case, nothing to log
    return modal.Sandbox.create(
        "sleep", "infinity", app=app, image=image, workdir=workdir, timeout=timeout
    )


def _create_caddy_sidecar(
    sb: modal.Sandbox,
    sidecar_image_id: str,
    api_url: str,
    token: str,
) -> None:
    sidecars = cast(_SidecarManager, sb._experimental_sidecars)
    sidecars.create(
        "caddy",
        "run",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
        name="caddy",
        image=modal.Image.from_id(sidecar_image_id),
        env={"SIDECAR_PORT": str(SIDECAR_PORT), "UPSTREAM_URL": api_url},
        secrets=[modal.Secret.from_dict({"DEVIN_OUTPOSTS_TOKEN": token})],
    )


def run_session(
    app: modal.App,
    image: modal.Image,
    session_id: str,
    *,
    pool_name: str,
    pool_id: str,
    api_url: str,
    sidecar_image_id: str,
    session_timeout_secs: int = SESSION_TIMEOUT_SECS,
) -> None:
    """Run one claimed session to completion (or suspension). Call this from the pool file's
    `run_session`.

    Devin requires every session repo checked out as a direct subdirectory of the working
    directory `devin worker start` runs from. `sb.exec()` below inherits this Sandbox's `workdir`
    (it doesn't pass its own), so wherever `image` clones repos to must match this constant.

    If the session comes back as `suspended` rather than fully done, its filesystem is snapshotted
    before the Sandbox is torn down, and that snapshot is what a later run_session for the same
    session_id resumes from -- per Devin's Outposts docs: "Terminate the machine when the worker
    exits... if your pool is resumable, snapshot the machine before terminating so you can restore
    it if the session resumes."
    """
    acceptor_id = f"modal-{pool_name}"
    token = os.environ["DEVIN_OUTPOSTS_TOKEN"]
    snapshot_name = _snapshot_name(pool_name, session_id)

    sb: modal.Sandbox | None = None
    try:
        sb = _create_sandbox(app, image, snapshot_name, "/root/workspace", session_timeout_secs)
        _create_caddy_sidecar(sb, sidecar_image_id, api_url, token)

        wait_proc = sb.exec(
            "sh",
            "-c",
            f"for i in $(seq 1 300); do "
            f"curl -s http://caddy:{SIDECAR_PORT}/ -o /dev/null && exit 0; sleep 0.2; done; "
            f"curl -v http://caddy:{SIDECAR_PORT}/ 2>&1; exit 1",
        )
        if wait_proc.wait() != 0:
            raise RuntimeError(
                "caddy sidecar did not become ready in time:\n" + wait_proc.stdout.read()
            )

        worker_env: dict[str, str | None] = {
            "DEVIN_API_URL": f"http://caddy:{SIDECAR_PORT}",
            "DEVIN_OUTPOSTS_TOKEN": DUMMY_TOKEN,
        }
        worker_proc = sb.exec(
            DEVIN_BIN,
            "worker",
            "start",
            "--session",
            session_id,
            "--pool",
            pool_id,
            "--acceptor-id",
            acceptor_id,
            env=worker_env,
        )
        for line in worker_proc.stdout:
            print(f"[{session_id}] {line}", end="")
        returncode = worker_proc.wait()
        print(f"[{session_id}] devin worker exited: {returncode}")

        status = _session_status(api_url, token, session_id, acceptor_id)
        if status == "suspended":
            print(f"[{session_id}] session suspended, snapshotting filesystem for resume")
            sb.snapshot_filesystem().publish(snapshot_name)
        # else: no explicit cleanup for a stale snapshot from an earlier suspend -- there's no
        # unpublish API, so a since-terminated session's old snapshot just ages out via the
        # underlying Image's own ttl (see snapshot_filesystem's ttl param, default 30 days).

        if returncode != 0:
            _release_safely(api_url, token, session_id, acceptor_id, "nonzero exit")
    except Exception as e:
        print(f"[{session_id}] run_session failed: {e}", file=sys.stderr)
        _release_safely(api_url, token, session_id, acceptor_id, "failure")
        raise
    finally:
        if sb is not None:
            sb.terminate()


def poll_and_dispatch(
    *,
    pool_name: str,
    pool_id: str,
    api_url: str,
    run_session_fn: SessionRunner,
    sidecar_image_id: str | None = None,
) -> None:
    """Poll for pending sessions, atomically claim each, and spawn `run_session_fn` for it.

    Call this from the pool file's `poll_and_dispatch`, passing its own `run_session` Function.
    """
    acceptor_id = f"modal-{pool_name}"
    poll_api_url = os.environ.get("DEVIN_API_URL", api_url)
    token = os.environ["DEVIN_OUTPOSTS_TOKEN"]

    try:
        pending = _api_request(
            poll_api_url,
            token,
            "GET",
            _query_path("/opbeta/outposts/devins", {"pool": pool_id, "phase": "pending"}),
        )
    except (urllib.error.URLError, ValueError) as e:
        print(f"poll failed: {e}", file=sys.stderr)
        return

    pending_items = tuple(_response_items(pending))
    if not pending_items:
        return

    if sidecar_image_id is None:
        try:
            sidecar_image_id = build_sidecar_image_id(pool_name)
        except Exception as e:
            print(f"sidecar image build failed before claiming sessions: {e}", file=sys.stderr)
            return

    for item in pending_items:
        session_id = _session_id_from_item(item)
        if session_id is None:
            print(f"pending item without metadata.session_id: {item}", file=sys.stderr)
            continue
        try:
            claim = _api_request(
                poll_api_url,
                token,
                "POST",
                _devin_path(session_id, "claim"),
                {"acceptor_id": acceptor_id},
            )
        except urllib.error.HTTPError as e:
            if e.code == 409:
                continue
            print(f"[{session_id}] claim failed: {e}", file=sys.stderr)
            continue
        except (urllib.error.URLError, ValueError) as e:
            print(f"[{session_id}] claim failed: {e}", file=sys.stderr)
            continue

        deadline = _claim_deadline_from_response(claim)
        print(f"[{session_id}] claimed, claim_deadline={deadline}, dispatching")
        try:
            run_session_fn.spawn(session_id, sidecar_image_id=sidecar_image_id)
        except Exception as e:
            print(f"[{session_id}] dispatch failed: {e}", file=sys.stderr)
            _release_safely(poll_api_url, token, session_id, acceptor_id, "dispatch failure")
