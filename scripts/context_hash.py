"""context_hash.py - fingerprint of the local files a Containerfile copies.

Usage:
    python3 context_hash.py CONTAINERFILE [--root DIR]

The content hash of an image must change when a file the build copies in
changes, not only when the Containerfile does. This prints one SHA-256 over
the relative path and the bytes of every file reached by the local
``COPY``/``ADD`` sources of the given Containerfile, or nothing when it
copies none (the hash formula is then left as it was).
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import cast

from check_build_context import local_sources

# Extra build contexts declared on a Containerfile, e.g. an overlay root the
# build mounts with --context: the files reached by the declared path are
# build inputs exactly like local COPY sources.
EXTRA_CTX_RE = re.compile(
    r"^LABEL\s+org\.daemoncores\.extra-contexts=(\S+)", re.MULTILINE
)


def extra_context_roots(containerfile: Path) -> list[str]:
    """List the context roots declared on a Containerfile.

    Args:
        containerfile: Image definition to scan.

    Returns:
        The declared directory paths (name:path pairs, path part only).
    """
    roots: list[str] = []
    for m in EXTRA_CTX_RE.findall(containerfile.read_text()):
        for pair in m.split(","):
            if ":" in pair:
                roots.append(pair.split(":", 1)[1].strip())
    return roots


def files_of(root: Path, source: str) -> list[Path]:
    """List the files a COPY/ADD source reaches.

    Args:
        root: Build context root.
        source: Source argument of the instruction (glob allowed).

    Returns:
        The matching files, directories walked recursively, sorted.
    """
    found: set[Path] = set()
    for match in root.glob(source.removeprefix("./")):
        if match.is_dir():
            found.update(p for p in match.rglob("*") if p.is_file())
        elif match.is_file():
            found.add(match)
    return sorted(found)


def context_hash(containerfile: Path, root: Path) -> str:
    """Hash the local files copied by a Containerfile.

    Args:
        containerfile: Image definition to scan.
        root: Build context root.

    Returns:
        The hex digest, or an empty string when nothing local is copied.
    """
    sources = local_sources(containerfile) + extra_context_roots(containerfile)
    if not sources:
        return ""
    digest = hashlib.sha256()
    seen: set[Path] = set()
    for source in sources:
        for path in files_of(root, source):
            if path in seen:
                continue
            seen.add(path)
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main() -> int:
    """Print the context hash of one Containerfile.

    Returns:
        Exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("containerfile", type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    containerfile = cast(Path, args.containerfile)
    root = cast(Path, args.root)
    print(context_hash(containerfile, root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
