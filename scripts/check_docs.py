"""check_docs.py - verify that the documentation still matches the tree.

Usage:
    python3 check_docs.py [--root DIR] [--docs DIR]

Kronn writes claims as ``[src: file: path:LINE]`` citations and Markdown
pages link to each other. Nothing else notices when a file moves or shrinks,
so the citations rot silently. This check fails on:

* a citation whose file does not exist;
* a citation whose line is past the end of the file or blank;
* a relative Markdown link whose target does not exist.

Pages under ``.kronn-citation-originals`` are Kronn backups and are skipped.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import cast

CITE_RE = re.compile(r"\[src: file: ([^\]:]+?)(?::(\d+)(?:-(\d+))?)?\]")
LINK_RE = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
SKIP = ".kronn-citation-originals"


def pages(docs: Path) -> list[Path]:
    """List the Markdown pages to verify.

    Args:
        docs: Documentation directory.

    Returns:
        Sorted Markdown files, Kronn backups excluded.
    """
    return sorted(p for p in docs.rglob("*.md") if SKIP not in p.parts)


def check_citation(root: Path, target: str, first: str, last: str) -> str:
    """Resolve one ``[src: file: ...]`` citation.

    Args:
        root: Repository root.
        target: Cited path relative to ``root``.
        first: First cited line, or an empty string.
        last: Last cited line, or an empty string.

    Returns:
        A defect description, or an empty string when the citation holds.
    """
    path = root / target
    if not path.is_file():
        return f"{target} does not exist"
    if not first:
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    end = int(last or first)
    if int(first) < 1 or end > len(lines):
        return f"{target}:{first} is past the end ({len(lines)} lines)"
    if not lines[int(first) - 1].strip():
        return f"{target}:{first} is a blank line"
    return ""


def check_page(root: Path, page: Path) -> list[str]:
    """Verify the citations and relative links of one page.

    Args:
        root: Repository root.
        page: Markdown page to scan.

    Returns:
        One line per defect.
    """
    found: list[str] = []
    rel = page.relative_to(root)
    for number, text in enumerate(
        page.read_text(encoding="utf-8").splitlines(), 1
    ):
        for target, first, last in cast(
            list[tuple[str, str, str]], CITE_RE.findall(text)
        ):
            defect = check_citation(root, target, first, last)
            if defect:
                found.append(f"{rel}:{number}: citation {defect}")
        for link in cast(list[str], LINK_RE.findall(text)):
            if "://" in link or link.startswith(("mailto:", "/")):
                continue
            if not (page.parent / link).exists():
                found.append(f"{rel}:{number}: broken link {link}")
    return found


class Options(argparse.Namespace):
    """Typed command-line options.

    Attributes:
        root: Repository to inspect.
        docs: Documentation directory, relative to the root.
    """

    root: Path
    docs: str


def main() -> int:
    """Entry point: print every defect and fail when there is one.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--docs", default="docs")
    args = parser.parse_args(namespace=Options())
    root = args.root.resolve()
    docs = root / args.docs
    found = [
        line for page in (pages(docs) if docs.is_dir() else [])
        for line in check_page(root, page)
    ]
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
