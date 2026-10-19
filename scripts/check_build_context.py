"""check_build_context.py - static checks of an image build context.

Usage:
    python3 check_build_context.py [--root DIR] [--packages DIR]

``podman build`` is not available everywhere, so this verifies the inputs the
build consumes: every local ``COPY`` source exists and is not excluded by
``.dockerignore``, and the package manifest (``packages.yml``) is coherent
(unique names, resolvable deps and sources, a build or upstream per entry,
every sourced ``lib/*.sh`` helper hashed in ``sources``).
"""

import argparse
import fnmatch
import re
import sys
from pathlib import Path
from typing import cast

import yaml

COPY_RE = re.compile(
    r"^(?:COPY|ADD)\s+(?!--from)(?:--\S+\s+)*(\S+)\s+\S+", re.MULTILINE
)
LIB_RE = re.compile(r"\blib/[\w.-]+\.sh\b")


def local_sources(containerfile: Path) -> list[str]:
    """List the local (non-URL) sources of the COPY/ADD instructions.

    Args:
        containerfile: Image definition to scan.

    Returns:
        The source argument of each local COPY or ADD instruction.
    """
    return [
        m for m in cast(list[str], COPY_RE.findall(containerfile.read_text()))
        if not m.startswith(("http://", "https://"))
    ]


def ignore_patterns(root: Path) -> list[str]:
    """Read the active patterns of ``.dockerignore``.

    Args:
        root: Repository root.

    Returns:
        The non-empty, non-comment lines of the file (none when absent).
    """
    path = root / ".dockerignore"
    if not path.is_file():
        return []
    lines = path.read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def check_containerfiles(root: Path) -> list[str]:
    """Check COPY sources against the tree and ``.dockerignore``.

    Args:
        root: Repository root.

    Returns:
        One line per defect.
    """
    found: list[str] = []
    patterns = [p.rstrip("/") for p in ignore_patterns(root)]
    for containerfile in sorted(root.glob("Containerfile*")):
        for source in local_sources(containerfile):
            pattern = source.removeprefix("./")
            if not list(root.glob(pattern)):
                found.append(f"{containerfile.name}: {source} missing")
            top = pattern.split("/")[0]
            found.extend(
                f"{containerfile.name}: {source} excluded by "
                f".dockerignore {ignored}" for ignored in patterns
                if fnmatch.fnmatch(top, ignored)
            )
    return found


def check_packages(packages_dir: Path) -> list[str]:
    """Check the coherence of ``packages.yml``.

    Args:
        packages_dir: Directory holding ``packages.yml``; ``sources`` are
            relative to it.

    Returns:
        One line per defect.
    """
    manifest = packages_dir / "packages.yml"
    if not manifest.is_file():
        return []
    loaded = cast(dict[str, object], yaml.safe_load(manifest.read_text()))
    entries = cast(list[dict[str, object]], loaded["packages"])
    names = [str(e["name"]) for e in entries]
    found: list[str] = []
    if not all(names) or len(names) != len(set(names)):
        found.append("package names must be unique and non-empty")
    for pkg in entries:
        name = str(pkg["name"])
        found.extend(
            f"{name} depends on unknown {dep}"
            for dep in cast(list[str],
                            pkg.get("deps") or []) if dep not in names
        )
        found.extend(
            f"{name}: {src} missing"
            for src in cast(list[str],
                            pkg.get("sources") or [])
            if not (packages_dir / src).exists()
        )
        build = str(pkg.get("build", "")).strip()
        if not pkg.get("upstream") and not build:
            found.append(f"{name}: neither upstream nor build")
        found.extend(
            f"{name}: {lib} used by build but not in sources"
            for lib in sorted(set(cast(list[str], LIB_RE.findall(build))))
            if lib not in cast(list[str],
                               pkg.get("sources") or [])
        )
    return found


class Options(argparse.Namespace):
    """Typed command-line options.

    Attributes:
        root: Repository to inspect.
        packages: Package builder directory, relative to the root.
    """

    root: Path
    packages: str


def main() -> int:
    """Entry point: print every defect and fail when there is one.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--packages", default="workflows/bootc-debs-builder")
    args = parser.parse_args(namespace=Options())
    root = args.root.resolve()
    found = check_containerfiles(root) + check_packages(root / args.packages)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
