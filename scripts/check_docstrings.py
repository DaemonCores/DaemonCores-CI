"""check_docstrings.py - Google docstring presence gate for Python files.

Usage:
    python3 check_docstrings.py [--root DIR] [PATH ...]

ruff ignores private callables and pydoclint only checks docstrings that
exist, so neither catches a helper or a test with no docstring at all. This
gate closes the gap: every module, class and function (private and test ones
included) is documented, and a callable with arguments, a return value or a
raised exception explains them in a Google-style section instead of a lone
summary line.

``PATH`` is a file or a directory searched recursively (default: ``scripts``
and ``tests`` under ``--root``).
"""

import argparse
import ast
import sys
from pathlib import Path

_DEF = (ast.FunctionDef, ast.AsyncFunctionDef)


def needs_sections(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Name the docstring sections a function must carry.

    Args:
        node: Function definition to inspect.

    Returns:
        The section headers (``Args:``, ``Returns:``, ``Raises:``,
        ``Yields:``) the docstring has to contain.
    """
    signature = node.args
    names = [
        a.arg for a in
        [*signature.posonlyargs, *signature.args, *signature.kwonlyargs]
        if a.arg not in {"self", "cls"}
    ]
    names += [a.arg for a in (signature.vararg, signature.kwarg) if a]
    body = [n for stmt in node.body for n in ast.walk(stmt)]
    sections: list[str] = []
    if names:
        sections.append("Args:")
    if any(isinstance(n, ast.Yield) for n in body):
        sections.append("Yields:")
    elif any(isinstance(n, ast.Return) and n.value for n in body):
        sections.append("Returns:")
    if any(isinstance(n, ast.Raise) for n in body):
        sections.append("Raises:")
    return sections


def problems(path: Path) -> list[str]:
    """Collect the docstring defects of one Python file.

    Args:
        path: Python file to inspect.

    Returns:
        One human-readable line per missing docstring or section.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    if not ast.get_docstring(tree):
        found.append(f"{path}: module docstring missing")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, *_DEF)):
            continue
        where = f"{path}:{node.lineno} {node.name}"
        doc = ast.get_docstring(node)
        if not doc:
            found.append(f"{where}: docstring missing")
        elif isinstance(node, _DEF):
            found.extend(
                f"{where}: {section} section missing"
                for section in needs_sections(node) if section not in doc
            )
    return found


def collect(root: Path, paths: list[str]) -> list[Path]:
    """Expand the requested paths into Python files.

    Args:
        root: Repository root.
        paths: Files or directories relative to ``root``; empty means the
            ``scripts`` and ``tests`` directories.

    Returns:
        Sorted Python files that exist.
    """
    wanted = [root / p
              for p in paths] if paths else [root / "scripts", root / "tests"]
    files: set[Path] = set()
    for item in wanted:
        if item.is_file():
            files.add(item)
        elif item.is_dir():
            files.update(item.rglob("*.py"))
    return sorted(files)


class Options(argparse.Namespace):
    """Typed command-line options.

    Attributes:
        root: Repository to inspect.
        paths: Files or directories to inspect.
    """

    root: Path
    paths: list[str]


def main() -> int:
    """Entry point: print every defect and fail when there is one.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(namespace=Options())
    found = [
        line for f in collect(args.root.resolve(), args.paths)
        for line in problems(f)
    ]
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
