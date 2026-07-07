"""`modal-devin` cyclopts CLI.

This package is a code generator, nothing more: `outpost create` fills in
templates/pool.py.tmpl with your pool's config and writes it out. The
generated file is plain Modal code and never imports modal_devin.
"""

import subprocess
from pathlib import Path
from string import Template

import cyclopts

app = cyclopts.App(name="modal-devin")
outpost = cyclopts.App(name="outpost", help="Manage Outposts pools running on Modal.")
app.command(outpost)

TEMPLATE_PATH = Path(__file__).parent / "templates" / "pool.py.tmpl"


@outpost.command
def create(
    name: str,
    *,
    pool_id: str = "",
    api_url: str = "https://api.beta.devinenterprise.com",
    secret_name: str = "devin-outposts-token",
    pools_dir: str = "pools",
):
    """Scaffold a new pool file under pools/<name>.py.

    Parameters
    ----------
    name: the pool's name (matches what `devin worker pool create` used, or
        will be created with -- see --pool-id).
    pool_id: the outpost_env-... id for an already-registered pool. If
        omitted, this runs `devin worker pool create <name>` for you.
    api_url: Devin API base URL (beta vs prod).
    secret_name: name of the Modal Secret holding DEVIN_OUTPOSTS_TOKEN.
        Create it with: modal secret create <secret_name> DEVIN_OUTPOSTS_TOKEN=...
    """
    if not pool_id:
        print(f"no --pool-id given, running: devin worker pool create {name}")
        result = subprocess.run(
            ["devin", "worker", "pool", "create", name, "--api-url", api_url],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"pool creation failed:\n{result.stderr}")
        pool_id = result.stdout.strip()
        print(f"created pool {name} -> {pool_id}")

    out_dir = Path(pools_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pool_file_stem = name.replace("-", "_")
    out_path = out_dir / f"{pool_file_stem}.py"

    if out_path.exists():
        raise SystemExit(f"{out_path} already exists, not overwriting")

    generated = Template(TEMPLATE_PATH.read_text()).substitute(
        pool_name=name,
        pool_id=pool_id,
        api_url=api_url,
        secret_name=secret_name,
        pool_file_stem=pool_file_stem,
    )
    out_path.write_text(generated)
    print(f"wrote {out_path} (self-contained -- no modal_devin import)")
    print(f"next: modal secret create {secret_name} DEVIN_OUTPOSTS_TOKEN=<token>")
    print(f"then: python3 {out_path}   (builds + publishes the sidecar image, one-time)")
    print(f"then: modal deploy {out_path}")


def run():
    app()


if __name__ == "__main__":
    run()
