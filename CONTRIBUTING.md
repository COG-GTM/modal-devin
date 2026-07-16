# Contributing

Thanks for helping improve `modal-devin`.

## Setup

```bash
uv sync --dev
```

## Architecture

`Worker` is the supported user-facing abstraction. Keep lifecycle and upstream
integration details behind the following private boundaries:

- `_config.py`: validated settings and resource identities;
- `_client.py`: Devin Outposts HTTP contract;
- `_runtime.py`: claim, dispatch, execution, and recovery state machine;
- `images.py`: composable public image helpers and sidecar compatibility;
- `worker.py`: the small public runtime and image API;
- `templates/outpost.py.tmpl`: the user-owned Modal application composition root.

Generated applications should use Modal's native `App` and `@app.function`
vocabulary. Do not expose sidecar image IDs, raw tokens, raw HTTP payloads, or
Modal Dicts through the public API.

`Worker.run_session()` deliberately derives its option signature from
`modal.Sandbox.create` with `ParamSpec`. Keep its runtime validation narrower than
Modal's full constructor: modal-devin owns command arguments, timeout, workdir, and
the readiness probe.

The scheduler may discover and spawn pending session IDs, but it must not acquire
their Outposts claims. Claiming belongs inside the spawned session invocation so a
Modal queue delay or retry cannot outlive the claim deadline. Unknown upstream
status values must fail closed and preserve a recovery snapshot.

Keep the generated scheduler at `max_containers=1`. Dispatch leases are acquired
before spawning and cleared when the session invocation starts; serialized scheduler
runs make stale-lease replacement deterministic. Scheduler failures must propagate so
Modal can alert and apply configured retries.

The sidecar image cache key includes the Caddy configuration, install recipe, and
`_SIDECAR_RECIPE_SCHEMA`. Bump that schema whenever `_sidecar_image()` changes in a
way not represented by the embedded inputs.

## Checks

Run the same checks as CI:

```bash
uv run coverage run -m pytest -q
uv run coverage report
uv run ruff format --check .
uv run ruff check .
uv run ty check --error all .
uv build
```

Tests that require live Modal or Devin credentials must be opt-in and must not
claim production sessions. Every runtime change should also have a network-free
lifecycle test covering cleanup and claim release.

To verify real Modal serialization and image hydration in a disposable workspace:

```bash
MODAL_DEVIN_RUN_MODAL_INTEGRATION=1 uv run pytest -m integration -q
```

The integration test creates an ephemeral Modal app and does not contact Devin.

## Security

Never put credentials in command arguments, logs, generated files, image-layer
metadata, or Git remote URLs. Report vulnerabilities using `SECURITY.md` rather
than a public issue.
