# modal-devin

Run Devin Outposts workers on Modal instead of dedicated VMs.

`modal-devin` generates a small Modal app for a Devin Outposts pool. The generated
file owns the Modal decorators, while the reusable polling, claiming, sandbox,
snapshot, and token-isolation logic stays in `modal_devin.outpost`.

## Install

```bash
uv tool install modal-devin
```

For local development from this repository:

```bash
uv sync --dev
uv run modal-devin --help
```

## Prerequisites

- Python 3.11 or newer.
- A configured Modal account: `uv run python -m modal setup`.
- A Devin service user key with Outposts access.
- The Devin CLI on your local machine if you want `modal-devin` to create the
  Devin pool for you.

Create the Modal secret used by generated pools without placing the token in
your shell history or process argv:

```bash
read -rsp "Devin Outposts token: " DEVIN_OUTPOSTS_TOKEN
printf "\n"
tmp_secret_file="$(mktemp)"
chmod 600 "$tmp_secret_file"
printf 'DEVIN_OUTPOSTS_TOKEN=%s\n' "$DEVIN_OUTPOSTS_TOKEN" > "$tmp_secret_file"
modal secret create --from-dotenv "$tmp_secret_file" devin-outposts-token
rm -f "$tmp_secret_file"
```

## Quick Start

Use an existing Devin Outposts pool id:

```bash
modal-devin outpost create my-pool --pool-id outpost_env-...
modal-devin outpost deploy pools/my_pool.py
```

Or run the command interactively and let it prompt for missing values:

```bash
modal-devin outpost create
```

For scripted setup, pass `--deploy` to create the file and deploy it with the
same Python environment that provides `modal-devin`:

```bash
modal-devin outpost create my-pool --pool-id outpost_env-... --deploy
```

The generated `pools/<name>.py` deploys two Modal functions:

- `poll_and_dispatch`: runs on a configurable Modal schedule, polls the Outposts
  API for pending sessions, resolves the sidecar image, claims a session, and
  spawns `run_session`.
- `run_session`: starts one Modal Sandbox for the claimed session, attaches a
  Caddy sidecar that injects the real Devin token, runs `devin worker start`,
  snapshots suspended sessions, and releases failed claims.

The generated pool reads worker settings with `pydantic-settings` at deploy
time. Defaults are sensible, but you can override them before deploy:

```bash
WORKER_POLL_INTERVAL_SECS=30 WORKER_SESSION_TIMEOUT_SECS=1800 modal-devin outpost deploy pools/my_pool.py
```

## Custom Worker Images

Edit the generated file before deploying if your worker needs repositories or
system packages:

```python
image = outpost.worker_image().run_commands(
    "git clone https://github.com/your-org/app /root/workspace/app"
)
```

For private GitHub repositories, use `clone_private_repo()` so the token is not
embedded in the clone URL, build logs, or the final `.git/config`:

```python
image = outpost.clone_private_repo(
    image,
    "https://github.com/your-org/app",
    "/root/workspace/app",
    token_secret=modal.Secret.from_name("github-clone-token"),
)
```

## Public API

The package ships `py.typed`. The intended public helpers are:

- `OutpostPoolConfig(name: str, pool_id: str, api_url: str = DEFAULT_API_URL)`
- `worker_image(...) -> modal.Image`
- `clone_private_repo(...) -> modal.Image`
- `build_sidecar_image_id(pool_name: str) -> str`
- `poll_and_dispatch(...) -> None`
- `run_session(...) -> None`

`poll_and_dispatch()` accepts any `SessionRunner` protocol implementation: an
object with `spawn(session_id: str, *, sidecar_image_id: str)`. New code should
pass `config=OutpostPoolConfig(...)`; the older `pool_name=...`, `pool_id=...`,
and `api_url=...` keyword arguments remain supported for generated files.

Runtime messages are emitted with the `modal_devin.outpost` logger. Generated
pools default `WORKER_LOG_LEVEL` to `INFO`.

## Security

The worker image installs the current Devin CLI through Devin's official install
script. The sidecar image installs the latest Linux amd64 Caddy release from
GitHub.

The real `DEVIN_OUTPOSTS_TOKEN` is mounted only into the Caddy sidecar. The
Devin worker process receives a dummy token and talks to the sidecar via
`DEVIN_API_URL`.

Modal sidecars are currently exposed by the Modal Python SDK as
`Sandbox._experimental_sidecars`. `modal-devin` keeps that dependency behind one
small typed wrapper, but it is still an upstream experimental API.

## Development

```bash
uv run pytest -q
uv run ruff check .
uv run ty check --error all .
uv build
```
