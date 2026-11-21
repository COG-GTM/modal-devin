# modal-devin

Run Devin Outposts sessions in isolated [Modal Sandboxes](https://modal.com/docs/guide/sandboxes)
that you configure and control.

## Get started

You need:

- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.11 or newer;
- a [Modal](https://modal.com) account (the wizard runs `modal setup` if this machine
  has no Modal credentials yet);
- a Devin organization with Outposts enabled, and a key from an **admin-scoped
  Enterprise service user** ([create one](https://docs.devin.ai/api-reference/authentication)).

Then run the interactive setup wizard from the project that will own the outpost:

```bash
uvx modal-devin init
```

The wizard configures Modal if needed, creates your outpost, stores its Devin
credential as a Modal Secret, generates the Modal application, and deploys it.

`uvx` runs the wizard without installing it. The other guides run commands such as
`uv run modal-devin doctor` and `uv run modal-devin deploy`, which need `modal-devin`
in your project's environment. Add it once. The project's `pyproject.toml` needs a
`[project]` table with `requires-python = ">=3.11"` or newer; if the project has no
`pyproject.toml` yet, `uv init --bare` creates one:

```bash
uv add modal-devin
```

Read the full documentation at [modal.com/docs/devin](https://modal.com/docs/devin).

> [!NOTE]
> `modal-devin` is alpha software. Until the first stable release, only the latest
> release on the default branch receives security fixes.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development and test workflow. Report
vulnerabilities through [GitHub private vulnerability reporting](https://github.com/modal-labs/modal-devin/security).
