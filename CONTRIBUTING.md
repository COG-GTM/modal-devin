# Contributing

Thanks for helping improve `modal-devin`.

## Local Setup

```bash
uv sync --dev
```

## Checks

Run the same checks as CI before opening a pull request:

```bash
uv run pytest -q
uv run ruff check .
uv run ty check --error all .
uv build
```

## Development Notes

- Keep the public API typed and backward-compatible unless the changelog calls
  out a breaking change.
- Prefer small, focused tests around network-free behavior. Code paths that need
  live Modal or Devin access should keep the impure boundary narrow and easy to
  exercise by hand.
- Do not put secrets in command arguments, logs, generated files, or image layer
  metadata.
