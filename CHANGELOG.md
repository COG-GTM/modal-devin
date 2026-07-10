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
- `Worker.run_session()` inherits Modal Sandbox keyword typing through `ParamSpec`
  while protecting runtime-owned lifecycle options.
- Deployed functions are named `scheduler` and `session` by operational role.
- Worker images remain composable between `Worker.base_image()` and the terminal
  `Worker.prepare_image()` source mount.
- Final session status failures preserve a recovery snapshot and fail the Modal
  invocation.
- Nonzero Devin worker exits now raise `WorkerExitedError`.
- Package installation is documented as a project dependency rather than an
  isolated tool installation.

### Removed

- The `Worker.app()` application factory and its Modal function-option mappings.
- The low-level public `poll_and_dispatch()`, `build_sidecar_image_id()`,
  `SessionRunner`, and `OutpostPoolConfig` surface.
- The redundant `modal-devin outpost` command group.

### Security

- The sidecar health check is served locally and no longer sends a token-bearing
  request to the upstream API.
- The credential-injecting proxy only forwards Outposts API paths.
- Worker output combines stdout and stderr so failures remain observable.
