# modal-devin

Run Devin Outposts sessions on Modal.

Devin's planning remains in Devin Cloud while commands, file edits, and repository
access run in isolated [Modal Sandboxes](https://modal.com/docs/guide/sandboxes) that
you control. `modal-devin` provides the orchestrator: it watches your outpost,
starts a Sandbox for each session, preserves suspended work, and cleans up when the
session ends.

> [!NOTE]
> `modal-devin` is alpha software. Until the first stable release, only the latest
> release on the default branch receives security fixes.

## When to use it

Use `modal-devin` when you want Devin sessions to:

- Run with custom tools, repositories, and system packages.
- Reach services and registries available from your Modal environment.
- Use Modal compute, memory, and region controls.
- Resume from a preserved workspace after suspension.
- Fit into an existing Python and Modal deployment workflow.

## How it fits into Outposts

Outposts separates the worker from the orchestrator:

- The **worker** executes one Devin session. `modal-devin` runs it inside a Modal
  Sandbox.
- The **orchestrator** finds queued sessions and manages their compute lifecycle.
  `modal-devin` deploys this layer as a Modal application.

Workers need outbound HTTPS access to Devin. They do not require inbound ports or a
public IP.

## Quick start

You need Python 3.11 or newer, a configured Modal account, an outpost ID, and
a Devin token authorized for that outpost.

```bash
uv add modal-devin
uv run modal setup
uv run modal-devin init my-outpost --outpost-id outpost_env-...
uv run modal-devin doctor
uv run modal-devin deploy outposts/my_outpost.py
```

Run `uv run modal-devin init` without arguments to create an outpost interactively.

The generated Python file is your Modal application. Edit it to choose the workspace
image, secrets, compute, region, retry policy, and schedule. The library manages the
Outposts and Sandbox lifecycle behind that application.

## Customize the workspace

Add packages and project source to the generated image block, before
`prepare_image()`:

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

## Documentation

Full tutorials, how-to guides, and reference docs live in this repository under
`tutorials/`, `guides/`, `explanation/`, and `reference/`. They're written for the
project's Mintlify site, which isn't published yet, so some page components won't
render when viewed directly on GitHub:

- [Deploy your first worker](tutorials/first-worker.mdx)
- [Build a project workspace](tutorials/custom-workspace.mdx)
- [Configure resources and regions](guides/resources-and-regions.mdx)
- [Operate a deployed worker](guides/operate-a-worker.mdx)
- [Security model](explanation/security.mdx)

## Security

Use a least-privilege Devin service user and separate credentials for environments
with different trust requirements. The session workload does not receive the raw
Outposts token in its environment, although it can exercise the Outposts capability
required to serve its assigned session.

Report vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/aaazzam/modal-devin/security).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development and test workflow.
