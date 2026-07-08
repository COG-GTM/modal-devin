"""Devin Outposts pool: shared poll/claim/dispatch/sidecar logic.

Mounted into pool images via `Image.add_local_python_source("modal_devin")` -- not pip-installed
at container runtime, just source-mounted so it ships with whatever's on disk at deploy time. Fix
a bug here once, redeploy every pool.

Modal requires `@app.function`-decorated functions to live at module (global) scope in the file
being deployed -- see the pool template. So this module doesn't own the decorated functions
themselves; it owns everything else (the sidecar image, the HTTP client, the actual run/poll
bodies) and the pool file's `run_session`/`poll_and_dispatch` are thin wrappers that call in here.
"""

import json
import os
import shlex
import sys
import urllib.error
import urllib.request

import modal

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

CHROME_PATH = "/usr/bin/chromium"


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
        .apt_install("git", "curl", "ca-certificates")
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
    """Clone a private repo into `dest` (e.g. /root/workspace/<repo>) using a token from
    `token_secret`, without leaving the token in the built image.

    DO NOT clone with the token embedded in the URL and stop there -- `git clone
    https://x-access-token:$TOKEN@host/...` writes that URL straight into `dest/.git/config`,
    which is then baked into the image layer. That image is what the Devin agent's own sandbox
    runs in, so an unscrubbed token in .git/config hands the agent a live credential to your git
    host. This clones with the token, then immediately strips it back out of `.git/config` via
    `git remote set-url`, in the same `run_commands` layer so the credential is never part of the
    committed filesystem. `token_secret`'s env var itself is build-scoped and doesn't persist into
    the final image regardless, but that alone doesn't save you from this file-level leak.

    `repo_url` should be the plain https URL (no embedded credentials), e.g.
    "https://github.com/your-org/app". Assumes a GitHub-style PAT or App installation token
    (the "x-access-token" username convention) -- adjust if cloning from a host that expects a
    different credential format.
    """
    if not token_env_var.isidentifier():
        raise ValueError(f"token_env_var must be a valid shell identifier, got: {token_env_var!r}")
    scheme, sep, rest = repo_url.partition("://")
    if not sep:
        raise ValueError(f"repo_url must be a full https:// URL, got: {repo_url!r}")

    # Single-quoted literals concatenated around a double-quoted `"$VAR"` expansion: only the
    # token reference is subject to shell expansion, nothing else in repo_url/dest is interpreted.
    # (An earlier version shlex.quote'd the whole URL as one single-quoted string, which silently
    # sent the literal text "$GIT_CLONE_TOKEN" instead of the actual secret to git -- broken.)
    auth_url = shlex.quote(f"{scheme}://x-access-token:") + f'"${token_env_var}"' + shlex.quote(f"@{rest}")
    secret_desc = token_secret.name or "<secret>"
    fail_msg = f"{token_env_var} is empty -- does secret {secret_desc} have a key named {token_env_var}?"
    return image.run_commands(
        f'test -n "${token_env_var}" '
        f"|| {{ echo {shlex.quote(fail_msg)} >&2; exit 1; }} "
        f"&& git clone {auth_url} {shlex.quote(dest)} "
        f"&& git -C {shlex.quote(dest)} remote set-url origin {shlex.quote(repo_url)}",
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
    return _sidecar_image().build(build_app).object_id


def _api_request(api_url, token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{api_url}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _release(api_url, token, session_id, acceptor_id):
    _api_request(
        api_url, token, "POST",
        f"/opbeta/outposts/devins/{session_id}/release",
        {"acceptor_id": acceptor_id},
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
):
    """Run one claimed session to completion. Call this from the pool file's `run_session`.

    Devin requires every session repo checked out as a direct subdirectory of the working
    directory `devin worker start` runs from. `sb.exec()` below inherits this Sandbox's `workdir`
    (it doesn't pass its own), so wherever `image` clones repos to must match this constant.
    """
    acceptor_id = f"modal-{pool_name}"
    token = os.environ["DEVIN_OUTPOSTS_TOKEN"]

    sb = modal.Sandbox.create(
        "sleep", "infinity",
        app=app, image=image, workdir="/root/workspace", timeout=session_timeout_secs,
    )
    try:
        sb._experimental_sidecars.create(
            "caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
            name="caddy",
            image=modal.Image.from_id(sidecar_image_id),
            env={"SIDECAR_PORT": str(SIDECAR_PORT), "UPSTREAM_URL": api_url},
            secrets=[modal.Secret.from_dict({"DEVIN_OUTPOSTS_TOKEN": token})],
        )

        wait_proc = sb.exec(
            "sh", "-c",
            f"for i in $(seq 1 300); do "
            f"curl -s http://caddy:{SIDECAR_PORT}/ -o /dev/null && exit 0; sleep 0.2; done; "
            f"curl -v http://caddy:{SIDECAR_PORT}/ 2>&1; exit 1",
        )
        if wait_proc.wait() != 0:
            raise RuntimeError(
                "caddy sidecar did not become ready in time:\n" + wait_proc.stdout.read()
            )

        worker_env = {
            "DEVIN_API_URL": f"http://caddy:{SIDECAR_PORT}",
            "DEVIN_OUTPOSTS_TOKEN": DUMMY_TOKEN,
        }
        worker_proc = sb.exec(
            DEVIN_BIN, "worker", "start",
            "--session", session_id,
            "--pool", pool_id,
            "--acceptor-id", acceptor_id,
            env=worker_env,
        )
        for line in worker_proc.stdout:
            print(f"[{session_id}] {line}", end="")
        returncode = worker_proc.wait()
        print(f"[{session_id}] devin worker exited: {returncode}")
        if returncode != 0:
            _release(api_url, token, session_id, acceptor_id)
            print(f"[{session_id}] released claim after nonzero exit")
    except Exception as e:
        print(f"[{session_id}] run_session failed: {e}", file=sys.stderr)
        _release(api_url, token, session_id, acceptor_id)
        print(f"[{session_id}] released claim after failure")
        raise
    finally:
        sb.terminate()


def poll_and_dispatch(*, pool_name: str, pool_id: str, api_url: str, run_session_fn):
    """Poll for pending sessions, atomically claim each, and spawn `run_session_fn` for it.

    Call this from the pool file's `poll_and_dispatch`, passing its own `run_session` Function.
    """
    acceptor_id = f"modal-{pool_name}"
    poll_api_url = os.environ.get("DEVIN_API_URL", api_url)
    token = os.environ["DEVIN_OUTPOSTS_TOKEN"]

    try:
        pending = _api_request(
            poll_api_url, token, "GET", f"/opbeta/outposts/devins?pool={pool_id}&phase=pending"
        )
    except urllib.error.URLError as e:
        print(f"poll failed: {e}", file=sys.stderr)
        return

    for item in pending.get("items", []):
        session_id = item["metadata"]["session_id"]
        try:
            claim = _api_request(
                poll_api_url, token, "POST",
                f"/opbeta/outposts/devins/{session_id}/claim",
                {"acceptor_id": acceptor_id},
            )
        except urllib.error.HTTPError as e:
            if e.code == 409:
                continue
            print(f"[{session_id}] claim failed: {e}", file=sys.stderr)
            continue

        deadline = claim.get("status", {}).get("claim_deadline")
        print(f"[{session_id}] claimed, claim_deadline={deadline}, dispatching")
        run_session_fn.spawn(session_id)
