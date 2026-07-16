from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def _public_docs() -> str:
    paths = [ROOT / "README.md"]
    paths.extend(ROOT.rglob("*.mdx"))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_docs_do_not_stage_secrets_in_files_or_command_arguments():
    docs = _public_docs()

    forbidden = (
        "--from-json",
        "--from-dotenv",
        "secret.json",
        "github-secret.json",
        "DEVIN_OUTPOSTS_TOKEN=your-",
    )
    for pattern in forbidden:
        assert pattern not in docs, f"unsafe credential setup pattern in docs: {pattern}"
