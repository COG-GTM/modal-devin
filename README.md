# modal-devin

Run Devin Outposts workers on Modal.

`modal-devin` generates an ordinary Modal application that belongs to your project.
The generated file owns deployment topology and policy; the library owns the Devin
worker protocol, Sandbox lifecycle, resumability, and token-isolating sidecar.

## Install

Add `modal-devin` to the project that owns the Modal application:

```bash
uv add modal-devin
```

Using a project dependency keeps imports, editor tooling, project packages, and
`modal deploy` in the same Python environment.

## Quick start

With an existing Devin Outposts pool ID:

```bash
uv run modal-devin init my-pool --pool-id outpost_env-...
uv run modal-devin doctor
uv run modal deploy pools/my_pool.py
```

Run `uv run modal-devin init` without arguments for the interactive setup flow.
It can invoke the Devin CLI to create the pool and can create the Modal secret
without putting the token in process arguments.

The generated file is application code and may be edited:

```python
import modal

from modal_devin import Worker

worker = Worker.from_env(
    "my-pool",
    pool_id="outpost_env-...",
)

app = modal.App(
    "modal-devin-my-pool",
    tags={"service": "modal-devin"},
)
devin_secret = modal.Secret.from_name("devin-outposts-token")
controller_image = worker.controller_image()

base_image = worker.base_image()
image = worker.prepare_image(base_image)


@app.function(
    name="session",
    image=image,
    secrets=[devin_secret],
    timeout=worker.session_function_timeout_seconds,
)
def session(session_id: str) -> None:
    worker.run_session(session_id, app=app, image=image)


@app.function(
    name="scheduler",
    image=controller_image,
    secrets=[devin_secret],
    schedule=modal.Period(seconds=worker.settings.scheduler_interval_seconds),
    max_containers=1,
)
def scheduler() -> None:
    worker.dispatch_pending_sessions(session.spawn)
```

The app, secret, decorators, schedule, function resources, and connection between
the scheduler and session function are deliberately visible. You can use standard
Modal documentation to change them or add more functions. The generator is not
needed after initialization.

The scheduler uses a small controller image containing only the Python runtime and
`modal_devin`; Devin, browser tooling, and project repositories stay in the session
image where they are needed.

## Customize the worker image

`Worker.base_image()` returns a normal, composable `modal.Image`. Complete every
user build step before calling `Worker.prepare_image()`:

```python
base_image = (
    worker.base_image()
    .apt_install("ripgrep")
    .run_commands(
        "git clone https://github.com/your-org/app /root/workspace/app"
    )
)

image = worker.prepare_image(base_image)
```

`prepare_image()` adds the `modal_devin` package as the terminal image operation,
as required by [Modal's local-source composition rules](https://modal.com/docs/sdk/py/latest/modal.Image#add_local_python_source).
Pass its result to the session function's `@app.function` decorator and to
`worker.run_session()`. `Worker.controller_image()` supplies the separate scheduler
image.

For a private GitHub repository, use `clone_private_repo()` so credentials do
not appear in the clone URL, build logs, or final Git remote:

```python
from modal_devin import clone_private_repo

base_image = clone_private_repo(
    worker.base_image(),
    "https://github.com/your-org/app",
    "/root/workspace/app",
    token_secret=modal.Secret.from_name("github-clone-token"),
)
image = worker.prepare_image(base_image)
```

## Customize deployment policy

The generated decorators are the customization surface for Modal function policy:

```python
@app.function(
    name="session",
    image=image,
    secrets=[devin_secret],
    timeout=worker.session_function_timeout_seconds,
    cpu=2,
    memory=4096,
    retries=2,
    region="eu-west",
)
def session(session_id: str) -> None:
    worker.run_session(
        session_id,
        app=app,
        image=image,
        cpu=4,
        memory=8192,
        region="eu-west",
    )
```

Decorator options configure the outer Modal function. Additional keywords passed to
`run_session()` configure the Devin Sandbox itself. Its keyword signature is inherited
from the installed `modal.Sandbox.create` using `ParamSpec`, so editor completion and
type checking follow Modal SDK updates. modal-devin rejects Sandbox command arguments
and protects its lifecycle-owned `timeout`, `workdir`, and `readiness_probe` options.

Scheduling is likewise normal Modal code. Keep `modal.Period` for continuous
polling, change its interval, or use `modal.Cron` when wall-clock alignment matters.

## Configuration

Pass `WorkerSettings` explicitly in Python or use `Worker.from_env()` to read
operational overrides:

| Environment variable | Default |
| --- | ---: |
| `MODAL_DEVIN_SCHEDULER_INTERVAL_SECONDS` | `30` |
| `MODAL_DEVIN_SESSION_TIMEOUT_SECONDS` | `1800` |
| `MODAL_DEVIN_API_TIMEOUT_SECONDS` | `30` |
| `MODAL_DEVIN_SNAPSHOT_TTL_SECONDS` | `2592000` (30 days) |
| `MODAL_DEVIN_STATUS_ATTEMPTS` | `3` |
| `MODAL_DEVIN_STATUS_RETRY_DELAY_SECONDS` | `1` |
| `MODAL_DEVIN_SANDBOX_READY_TIMEOUT_SECONDS` | `120` |
| `MODAL_DEVIN_SIDECAR_READY_TIMEOUT_SECONDS` | `60` |
| `MODAL_DEVIN_SNAPSHOT_TIMEOUT_SECONDS` | `120` |
| `MODAL_DEVIN_LOG_LEVEL` | `INFO` |

Set `MODAL_DEVIN_SNAPSHOT_TTL_SECONDS=none` to retain snapshots indefinitely.
`MODAL_DEVIN_SESSION_TIMEOUT_SECONDS` applies to the Devin worker command itself. The
Sandbox receives a larger derived lifetime for readiness, sidecar startup, final-status
retry, snapshotting, and cleanup. `session_function_timeout_seconds` additionally
budgets claim and release requests for the outer function; you may set a larger value
in the decorator.

## Resumability

Filesystem snapshots are stored by Modal image ID in a dedicated Modal Dict,
indexed under a digest of the Devin session ID. This keeps arbitrary or long session
IDs out of persistent keys and follows [Modal's documented persistence pattern](https://modal.com/docs/guide/sandbox-snapshots#persisting-sandbox-state).

The runtime:

1. reserves pending IDs before dispatch, preventing each scheduler tick from enqueueing
   the same session while preserving a bounded retry lease;
2. dispatches without holding an Outposts claim while Modal queues the invocation;
3. claims after the session function starts, making retries safe;
4. restores a stored snapshot and waits for the Sandbox readiness probe;
5. removes an expired snapshot reference and falls back to the base image;
6. validates final status against the known Outposts protocol and retries lookups;
7. takes a recovery snapshot when status is unavailable, unknown, or active;
8. removes the snapshot mapping after a completed session.

The scheduler refreshes snapshot-index entries once per day so Modal Dict's inactivity
expiry cannot outlive the configured snapshot retention while the deployment is active.
Keep the scheduler deployed when relying on 30-day or indefinite resumability.

A nonzero Devin worker exit raises `WorkerExitedError`, so Modal records a failed
invocation instead of a successful one.

## Security model

The real `DEVIN_OUTPOSTS_TOKEN` is mounted into a Caddy sidecar, not the main
Sandbox. The Devin worker receives a non-secret placeholder token and sends its
Outposts requests through the sidecar.

The sidecar is a capability boundary, not a claim that the Sandbox cannot use
the credential. Sandbox processes can exercise the credential through the
proxy, but the proxy only forwards `/opbeta/outposts/*`; other paths receive
HTTP 403. Use a least-privilege Devin service user.

The current worker image executes Devin's official CLI installation script, and
the sidecar image resolves the latest Linux amd64 Caddy release at build time.

Modal currently exposes Sandbox sidecars through
`Sandbox._experimental_sidecars`. All access is isolated in one compatibility
adapter, and `modal-devin doctor` checks that the installed SDK exposes it.

## Public API

The package ships `py.typed`. Its supported surface is intentionally small:

- `Worker` with `base_image()`, `prepare_image()`, `controller_image()`,
  `run_session()`, and `dispatch_pending_sessions()`;
- `WorkerSettings`;
- `clone_private_repo()`;
- the `ModalDevinError` exception hierarchy.

Generated files remain dependent on `modal-devin`, as ordinary applications depend
on runtime libraries, but they are independent of the generator and can be copied,
handwritten, or extended directly.

## Development

```bash
uv sync --dev
uv run coverage run -m pytest -q
uv run coverage report
uv run ruff format --check .
uv run ruff check .
uv run ty check --error all .
uv build
```
