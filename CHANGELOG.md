# Changelog

All notable changes are documented here. The project follows Keep a Changelog
and uses semantic versioning.

## Unreleased

### Added

- `Worker` as an immutable configured runtime with durable image, session, and
  dispatch operations.
- Validated `WorkerSettings` with explicit `MODAL_DEVIN_*` environment loading.
- Typed public exceptions for configuration, compatibility, API, status, and
  worker-process failures.
- `modal-devin init`, `deploy`, and `doctor` project-level commands.
- Modal Dict-backed filesystem snapshot persistence.

### Changed

- Generated files now own the `modal.App`, secret, image composition, function
  decorators, schedule, and connection between the scheduler and session function.
- Session and scheduler bodies are thin adapters into `Worker.run_session()` and
  `Worker.dispatch_pending_sessions()`.
- Claims are acquired inside the spawned session invocation rather than before the
  asynchronous Modal dispatch, so queue delays and function retries do not reuse a
  stale claim.
- Outposts session states are parsed into a closed protocol type; malformed or new
  states preserve recovery data instead of being treated as completed.
- Scheduler functions use a lightweight controller image instead of the full Devin
  worker image.
- Sidecar image cache entries are versioned by the sidecar recipe digest.
- `Worker.run_session()` inherits Modal Sandbox keyword typing through `ParamSpec`
  while protecting runtime-owned lifecycle options.
- Deployed functions are named `scheduler` and `session` by operational role.
- Worker images remain composable between `Worker.base_image()` and the terminal
  `Worker.prepare_image()` source mount.
- Final session status failures preserve a recovery snapshot and fail the Modal
  invocation.
- Nonzero Devin worker exits now raise `WorkerExitedError`.
- Outer function timeouts now account for status requests and backoff, sidecar
  readiness, snapshot creation, claim release, and cleanup.
- The Devin worker command receives the configured session timeout directly, while
  the containing Sandbox receives a larger lifecycle budget for startup and recovery.
- Scheduler transport, protocol, image-build, and dispatch failures now fail the
  scheduled invocation after attempting every independent dispatch.
- Pending session dispatches use bounded leases to avoid duplicate Modal queue entries.
- Snapshot index entries are refreshed daily while the scheduler is deployed, keeping
  their retention aligned with 30-day or indefinite filesystem snapshots.
- Package installation is documented as a project dependency rather than an
  isolated tool installation.
- Scheduler invocations now surface protocol, sidecar-build, and dispatch failures;
  all pending sessions are still attempted before dispatch errors are reported.
- Devin CLI installation failures now stop worker image builds at the failing
  installer step.
- `pool`/`pool_id` terminology is renamed to `outpost`/`outpost_id` throughout the
  public API, CLI, and generated files, matching Devin Outposts' current API
  vocabulary: `Worker(pool_id=...)` is now `Worker(outpost_id=...)`, the `init`
  `--pool-id` flag is now `--outpost-id`, and the default `init` output directory
  is `outposts/` instead of `pools/`.
- `modal-devin init` now creates and rolls back outposts with a direct call to the
  Devin Outposts API (`POST`/`DELETE /opbeta/outposts`) instead of shelling out to
  the `devin` CLI's `worker pool create`. The local `devin` CLI is no longer
  required to run `init`, `deploy`, or `doctor` -- it's still required inside the
  deployed worker image at runtime.

### Removed

- The `Worker.app()` application factory and its Modal function-option mappings.
- The low-level public `poll_and_dispatch()`, `build_sidecar_image_id()`,
  `SessionRunner`, and `OutpostPoolConfig` surface.
- The redundant `modal-devin outpost` command group.
- The `clone_private_repo()` image-build helper.

### Fixed

- `modal-devin init`'s outpost creation no longer returns `405 Method Not Allowed`
  against the Devin Outposts beta API: the `devin` CLI's `worker pool create`
  subcommand targeted a route the API backend had already retired in favor of
  `POST /opbeta/outposts`.
- The `init` rollback path's outpost-deletion request was hitting a nonexistent URL
  (`/outposts/pools/{id}`, missing the `/opbeta` prefix) and silently failing every
  time; it now calls the correct `DELETE /opbeta/outposts/{outpost_id}`.
- Worker image builds no longer fail on the `devin` CLI's own `install.sh`, whose last
  step unconditionally runs an interactive `devin setup` OAuth wizard that always
  fails without a TTY. The install step now succeeds or fails based on whether the
  `devin` binary is actually present and executable afterward, not on that wizard's
  incidental exit code -- a genuinely failed install still fails the build.

### Security

- Interactive Modal Secret creation now uses the public Modal SDK directly, keeping
  the Devin service key in memory instead of writing a temporary JSON file or
  invoking a secret-creation subprocess.
- Secret discovery now uses the public Modal SDK instead of parsing subprocess JSON,
  and credential setup documentation no longer stages tokens in local files.
- The sidecar health check is served locally and no longer sends a token-bearing
  request to the upstream API.
- The credential-injecting proxy only forwards Outposts API paths.
- Worker output combines stdout and stderr so failures remain observable.
