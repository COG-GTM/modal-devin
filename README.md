# modal-devin

Run Devin Outposts sessions in isolated [Modal Sandboxes](https://modal.com/docs/guide/sandboxes)
that you configure and control.

## Get started

[Install uv](https://docs.astral.sh/uv/getting-started/installation/) if you do not
already have it, then run the interactive setup wizard:

```bash
uvx modal-devin init
```

The wizard configures Modal if needed, creates your outpost, stores its Devin
credential as a Modal Secret, generates the Modal application, and deploys it.

Read the full documentation at [modal.com/docs/devin](https://modal.com/docs/devin).

> [!NOTE]
> `modal-devin` is alpha software. Until the first stable release, only the latest
> release on the default branch receives security fixes.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development and test workflow. Report
vulnerabilities through [GitHub private vulnerability reporting](https://github.com/aaazzam/modal-devin/security).
