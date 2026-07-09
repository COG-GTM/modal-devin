# Changelog

All notable changes to this project should be documented in this file.

The format follows Keep a Changelog, and this project uses semantic versioning.

## Unreleased

### Added

- `OutpostPoolConfig` as the preferred typed configuration object for pool
  helpers.
- `modal-devin outpost deploy` and `modal-devin outpost create --deploy` so
  installed users deploy generated pools from the same Python environment.
- GitHub Actions CI for tests, linting, type checking, and package builds.
- Contributing notes for local development and security expectations.

### Changed

- Generated pools use `OutpostPoolConfig` and configure logging with
  `WORKER_LOG_LEVEL`.
- Runtime library messages now use the `modal_devin.outpost` logger instead of
  writing directly to stdout or stderr.
- Package metadata now declares Python 3.11, 3.12, and 3.13 support.

### Security

- Interactive Modal secret creation now uses a temporary JSON file instead of
  passing `DEVIN_OUTPOSTS_TOKEN` in subprocess arguments.
