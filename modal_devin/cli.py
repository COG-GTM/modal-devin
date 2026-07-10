"""The modal-devin project CLI.

``modal-devin init`` prompts for missing values on an interactive terminal and
requires explicit values in scripts and CI.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Annotated, TextIO, TypedDict, Unpack, cast

import cyclopts
import modal
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.text import Text

from modal_devin import ConfigurationError, Worker
from modal_devin._config import DEFAULT_API_TIMEOUT_SECONDS, DEFAULT_API_URL

app = cyclopts.App(name="modal-devin")

TEMPLATE = files("modal_devin").joinpath("templates", "pool.py.tmpl")
DEVIN_TOKEN_URL = "https://docs.devin.ai/api-reference/authentication"
_MODULE_STEM_RE = re.compile(r"[^0-9A-Za-z_]+")

# create-next-app-style prompts: "? Question › answer" instead of Rich's default "Question: ".
_PROMPT_SUFFIX = Text(" › ", style="dim")
_console = Console()


class _PromptKwargs(TypedDict, total=False):
    console: Console
    password: bool
    choices: list[str]
    case_sensitive: bool
    show_default: bool
    show_choices: bool
    default: object
    stream: TextIO


PoolNameArg = Annotated[
    str,
    cyclopts.Parameter(help="Human-readable Devin worker pool name.", show_default=False),
]
PoolIdOption = Annotated[
    str,
    cyclopts.Parameter(
        help="Existing Devin Outposts pool id. Omit to create a pool with the Devin CLI.",
        show_default=False,
    ),
]
PoolFileArg = Annotated[
    Path,
    cyclopts.Parameter(help="Generated Modal pool file to deploy."),
]
ApiUrlOption = Annotated[
    str,
    cyclopts.Parameter(help="Base URL for the Devin API."),
]
SecretNameOption = Annotated[
    str,
    cyclopts.Parameter(help="Modal Secret name containing DEVIN_OUTPOSTS_TOKEN."),
]
PoolsDirOption = Annotated[
    Path,
    cyclopts.Parameter(help="Directory where the generated Modal pool file is written."),
]
DeployOption = Annotated[
    bool | None,
    cyclopts.Parameter(help="Deploy the generated pool file after writing it.", show_default=False),
]


class _WizardPrompt(Prompt):
    prompt_suffix = _PROMPT_SUFFIX


class _WizardConfirm(Confirm):
    prompt_suffix = _PROMPT_SUFFIX


def _erase_last_line() -> None:
    if not _interactive():
        return
    _console.file.write("\x1b[1A\x1b[2K")


def _mark_answered(question: str, shown_value: str) -> None:
    """Rewrite the just-answered prompt line in place, swapping the `?` for a `✔` -- only called
    from _ask/_confirm, which are only reached when stdout is already known to be a real tty."""
    _erase_last_line()
    _console.print(f"[bold green]✔[/bold green] [bold]{question}[/bold] [dim]›[/dim] {shown_value}")


def _ask(question: str, **kwargs: Unpack[_PromptKwargs]) -> str:
    answer = cast(
        str,
        _WizardPrompt.ask(f"[bold green]?[/bold green] [bold]{question}[/bold]", **kwargs),
    ).strip()
    if kwargs.get("password"):
        # Fixed-width mask, not "*" * len(answer): a real token is long enough that echoing its
        # length back as a wall of asterisks is both ugly and a pointless bit of a leak.
        shown = "•" * 8 if answer else "[dim]skipped[/dim]"
    else:
        shown = answer or "[dim]skipped[/dim]"
    _mark_answered(question, shown)
    return answer


def _confirm(question: str, **kwargs: Unpack[_PromptKwargs]) -> bool:
    answer = bool(
        _WizardConfirm.ask(f"[bold green]?[/bold green] [bold]{question}[/bold]", **kwargs)
    )
    _mark_answered(question, "yes" if answer else "no")
    return answer


def _step_start(running: str) -> None:
    """Print a dim in-progress line; pair with _step_done/_step_failed to redraw it as a
    checkmark or cross once the step resolves -- create-next-app style."""
    if _interactive():
        _console.print(f"[dim]{running}[/dim]")
    else:
        _console.print(running)


def _step_done(text: str) -> None:
    if _interactive():
        _erase_last_line()
    _console.print(f"[bold green]✔[/bold green] {text}")


def _step_failed(text: str) -> None:
    if _interactive():
        _erase_last_line()
    _console.print(f"[bold red]✖[/bold red] {text}")


def _done(text: str) -> None:
    """A completed-step line for actions with no visible delay (no matching _step_start), so
    nothing needs erasing first."""
    _console.print(f"[bold green]✔[/bold green] {text}")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _modal(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `modal` via the current interpreter rather than a bare `modal` on PATH: `modal` is
    a declared dependency, so this consistently uses the project's selected environment."""
    return subprocess.run(
        [sys.executable, "-m", "modal", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_with_tail(argv: Sequence[str], window: int = 10) -> int:
    """Run a subprocess, showing only the last `window` lines of its output at a time -- enough
    to see it's alive and what it's doing, without flooding the terminal with a full build log.
    The final `window` lines stick around afterward (e.g. modal deploy's own success/URL line)."""
    lines: deque[str] = deque(maxlen=window)
    all_lines: list[str] = []
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    if proc.stdout is None:  # pragma: no cover - guaranteed by stdout=PIPE
        raise RuntimeError("failed to capture subprocess output")
    try:
        with Live(console=_console, refresh_per_second=12, transient=True) as live:
            for line in proc.stdout:
                rendered = line.rstrip()
                lines.append(rendered)
                all_lines.append(rendered)
                live.update(Text("\n".join(lines), style="dim"))
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    visible_lines = all_lines if proc.returncode else list(lines)
    if visible_lines:
        _console.print(Text("\n".join(visible_lines), style="dim"))
    return proc.returncode


def _modal_deploy(pool_file: Path) -> int:
    argv = [sys.executable, "-m", "modal", "deploy", str(pool_file)]
    if _interactive():
        return _run_with_tail(argv)
    return subprocess.run(argv).returncode


def _existing_secret_names() -> set[str] | None:
    """Names of secrets already in the workspace, or None if the check itself failed (not
    logged in, ...) -- callers should fall back to manual instructions."""
    try:
        result = _modal("secret", "list", "--json")
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    names: set[str] = set()
    for item in payload:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _create_modal_secret(secret_name: str, token: str) -> subprocess.CompletedProcess[str]:
    """Create a Modal secret without placing the token in the subprocess argv."""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="modal-devin-secret-",
            suffix=".json",
            delete=False,
        ) as secret_file:
            tmp_path = secret_file.name
            os.chmod(tmp_path, 0o600)
            json.dump({"DEVIN_OUTPOSTS_TOKEN": token}, secret_file)
            secret_file.write("\n")
        return _modal("secret", "create", "--from-json", tmp_path, secret_name)
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


def _delete_pool(api_url: str, token: str | None, pool_id: str) -> bool:
    """Best-effort rollback of a pool `create` just registered with Devin, for when a later
    (local, avoidable) step fails before the pool file is on disk -- otherwise the command exits
    having created a live pool with no local record of it. Returns whether the delete succeeded;
    callers should print manual cleanup instructions on False."""
    if not token:
        return False
    req = urllib.request.Request(f"{api_url.rstrip('/')}/outposts/pools/{pool_id}", method="DELETE")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_API_TIMEOUT_SECONDS):
            return True
    except urllib.error.URLError:
        return False


def _modal_is_configured() -> bool:
    """Whether Modal accepts the token available to the current Python environment."""
    try:
        return _modal("token", "info").returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _module_stem(name: str) -> str:
    stem = _MODULE_STEM_RE.sub("_", name).strip("_").lower()
    if not stem:
        raise SystemExit("NAME must contain at least one ASCII letter or digit")
    if stem[0].isdigit():
        stem = f"pool_{stem}"
    return stem


def _python_literal(value: str) -> str:
    return repr(value)


@app.command
def deploy(pool_file: PoolFileArg) -> None:
    """Deploy a generated Modal pool file using modal-devin's Python environment."""
    returncode = _modal_deploy(pool_file)
    if returncode != 0:
        raise SystemExit(returncode)


@app.command(name="init")
def init_worker(
    name: PoolNameArg = "",
    *,
    pool_id: PoolIdOption = "",
    api_url: ApiUrlOption = DEFAULT_API_URL,
    secret_name: SecretNameOption = "devin-outposts-token",
    pools_dir: PoolsDirOption = Path("pools"),
    deploy: DeployOption = None,
) -> None:
    """Scaffold a Modal pool file for Devin Outposts."""
    interactive = _interactive()

    if interactive and not _modal_is_configured() and _confirm("Set up Modal now?", default=True):
        subprocess.run([sys.executable, "-m", "modal", "setup"])
        if _modal_is_configured():
            _done("Modal is set up")

    while not name:
        if not interactive:
            raise SystemExit("NAME is required (pass it as an argument, or run interactively)")
        name = _ask("What is your pool named?")

    if not secret_name.strip():
        raise SystemExit("secret_name must not be empty")

    try:
        validated_worker = Worker(
            name,
            pool_id=pool_id or "pending",
            api_url=api_url,
        )
    except ConfigurationError as error:
        raise SystemExit(str(error)) from error

    # Resolve and validate the output path *before* creating anything remotely, so the one
    # foreseeable, locally-checkable failure (name collision) never even reaches the API.
    # Anything that fails after that point (mkdir, template write, ...) rolls the pool back via
    # DELETE /outposts/pools/{pool_id} instead, so `create` doesn't leave a live pool behind with
    # no local file to show for it.
    out_dir = pools_dir
    out_path = out_dir / f"{_module_stem(name)}.py"
    if out_path.exists():
        raise SystemExit(f"{out_path} already exists, not overwriting")

    token = None  # the Devin Service User Key (env var DEVIN_OUTPOSTS_TOKEN), if we get one
    created_pool_id = None  # set once we register a *new* pool -- rolled back if a later step fails

    if not pool_id:
        token = os.environ.get("DEVIN_OUTPOSTS_TOKEN")
        if not token and interactive:
            _console.print(f"[dim]Grab a Devin Service User Key: {DEVIN_TOKEN_URL}[/dim]")
            token = _ask("Devin Service User Key", password=True)

        _step_start(f"Running devin worker pool create {name}...")
        env = {**os.environ, "DEVIN_OUTPOSTS_TOKEN": token} if token else None
        try:
            result = subprocess.run(
                ["devin", "worker", "pool", "create", name, "--api-url", api_url],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
        except FileNotFoundError as e:
            _step_failed("devin worker pool create failed")
            raise SystemExit(
                "`devin` CLI not found on PATH -- install it with:\n"
                "  curl -fsSL https://cli.devin.ai/install.sh | bash"
            ) from e
        except subprocess.TimeoutExpired as error:
            _step_failed("devin worker pool create timed out")
            raise SystemExit("Devin pool creation did not finish within 120 seconds") from error
        if result.returncode != 0:
            _step_failed(f"devin worker pool create {name} failed")
            raise SystemExit(result.stderr)
        pool_id = result.stdout.strip()
        if not pool_id:
            raise SystemExit("Devin CLI succeeded without returning a pool ID")
        created_pool_id = pool_id
        _step_done(
            f"Created pool [bold]{escape(name)}[/bold] [dim]→[/dim] [cyan]{escape(pool_id)}[/cyan]"
        )

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        generated = Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
            app_name=_python_literal(validated_worker.app_name),
            pool_name=_python_literal(name),
            pool_id=_python_literal(pool_id),
            api_url=_python_literal(api_url),
            secret_name=_python_literal(secret_name),
        )
        out_path.write_text(generated, encoding="utf-8")
    except OSError as e:
        if created_pool_id and _delete_pool(api_url, token, created_pool_id):
            raise SystemExit(
                f"Failed to write {out_path}, rolled back pool {created_pool_id}: {e}"
            ) from e
        elif created_pool_id:
            raise SystemExit(
                f"Failed to write {out_path}: {e}\n"
                f"Also failed to roll back pool {name} ({created_pool_id}) -- "
                f"delete it from the Devin dashboard or with the Outposts API."
            ) from e
        raise SystemExit(f"Failed to write {out_path}: {e}") from e
    _done(f"Wrote [cyan]{escape(str(out_path))}[/cyan]")

    existing_secrets = _existing_secret_names()
    secret_created = False
    if existing_secrets is not None and secret_name in existing_secrets:
        _done(f"Modal secret [bold]{escape(secret_name)}[/bold] already exists")
    elif interactive:
        if not token:
            _console.print(f"[dim]Grab a Devin Service User Key: {DEVIN_TOKEN_URL}[/dim]")
            token = _ask(
                "Devin Service User Key [dim](leave blank to skip)[/dim]",
                password=True,
                default="",
                show_default=False,
            )
        if token and _confirm(f"Create Modal secret {secret_name} now?", default=True):
            _step_start(f"Creating Modal secret {secret_name}...")
            result = _create_modal_secret(secret_name, token)
            if result.returncode != 0:
                _step_failed(f"Modal secret {secret_name} creation failed")
                _console.print(result.stderr)
            else:
                _step_done(f"Created Modal secret [bold]{secret_name}[/bold]")
                secret_created = True

    if not secret_created and secret_name not in (existing_secrets or set()):
        _console.print(
            f"[dim]next:[/dim] modal secret create --from-json "
            f"[cyan]/path/to/secret.json[/cyan] {secret_name}"
        )

    should_deploy = (
        deploy
        if deploy is not None
        else interactive
        and _confirm(
            f"Deploy {name} now?",
            default=True,
        )
    )
    if should_deploy:
        _console.print(f"[dim]Running modal deploy {out_path}...[/dim]")
        returncode = _modal_deploy(out_path)
        if returncode == 0:
            _done(f"Deployed [bold]{name}[/bold]")
        else:
            _console.print(f"[bold red]✖[/bold red] modal deploy failed (exit code {returncode})")
            raise SystemExit(returncode)
    else:
        _console.print(f"[dim]then:[/dim] modal-devin deploy [cyan]{escape(str(out_path))}[/cyan]")


@app.command
def doctor(
    *,
    secret_name: SecretNameOption = "devin-outposts-token",
) -> None:
    """Check local Modal, Devin, and worker-runtime prerequisites."""
    failures = 0

    if hasattr(modal.Sandbox, "_experimental_sidecars"):
        _done("Installed Modal SDK exposes Sandbox sidecars")
    else:
        _step_failed("Installed Modal SDK does not expose Sandbox sidecars")
        failures += 1

    if _modal_is_configured():
        _done("Modal credentials are valid")
    else:
        _step_failed("Modal credentials are missing or invalid; run `modal setup`")
        failures += 1

    secrets = _existing_secret_names()
    if secrets is None:
        _step_failed("Could not inspect Modal secrets")
        failures += 1
    elif secret_name in secrets:
        _done(f"Modal secret [bold]{escape(secret_name)}[/bold] exists")
    else:
        _step_failed(f"Modal secret {escape(secret_name)} does not exist")
        failures += 1

    devin_path = shutil.which("devin")
    if devin_path:
        _done(f"Devin CLI found at [cyan]{escape(devin_path)}[/cyan]")
    else:
        _console.print("[yellow]![/yellow] Devin CLI not found (only needed to create new pools)")

    if failures:
        raise SystemExit(1)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
