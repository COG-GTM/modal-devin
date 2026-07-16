from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
_DOC_DIRS = ("tutorials", "guides", "explanation", "reference")


def _nav_pages() -> list[str]:
    docs_json = json.loads((ROOT / "docs.json").read_text(encoding="utf-8"))
    pages = []
    for group in docs_json["navigation"]["groups"]:
        pages.extend(group["pages"])
    return pages


def _doc_slugs() -> set[str]:
    slugs = {"index"}
    for directory in _DOC_DIRS:
        for path in (ROOT / directory).glob("*.mdx"):
            slugs.add(f"{directory}/{path.stem}")
    return slugs


def test_navigation_pages_exist_on_disk():
    missing = [page for page in _nav_pages() if not (ROOT / f"{page}.mdx").exists()]
    assert not missing, f"docs.json references pages missing from disk: {missing}"


def test_every_doc_page_is_in_navigation():
    orphaned = _doc_slugs() - set(_nav_pages())
    assert not orphaned, f".mdx pages missing from docs.json navigation: {orphaned}"
