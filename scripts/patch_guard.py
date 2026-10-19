"""patch_guard.py - sha256 guard of the kernel files the patches touch.

Usage:
    python3 patch_guard.py --tree DIR --lock FILE [--update] PATCH [PATCH ...]

A patch written against one kernel can silently mis-apply on the next one even
when ``patch`` accepts it. The lock file records, for every pre-existing file
the patches modify, the sha256 it had in the kernel the patches were last
validated against. The guard hashes those files in the pristine ``--tree``,
before any patch is applied, and refuses the build when one differs from the
lock or has no reference. With ``--update`` it rewrites the lock from the tree
instead, merging into the existing lock so several runs (one per class and
architecture) build the full reference; a CI job runs it to propose the new
references.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import cast

TARGET = re.compile(r"^\+\+\+ (?:b/)?(\S+)")
SOURCE = re.compile(r"^--- (\S+)")


def touched_files(patch: str) -> set[str]:
    """List the pre-existing files a patch modifies.

    Files the patch creates (source ``/dev/null``) have no reference state and
    are left out.

    Args:
        patch: Unified diff text.

    Returns:
        The paths relative to the kernel root, without the ``b/`` prefix.
    """
    found: set[str] = set()
    created = False
    for line in patch.splitlines():
        if match := SOURCE.match(line):
            created = match.group(1) == "/dev/null"
        elif (match := TARGET.match(line)) and not created \
                and match.group(1) != "/dev/null":
            found.add(match.group(1))
    return found


def file_hashes(tree: Path, files: set[str]) -> dict[str, str]:
    """Hash files of a kernel tree.

    Args:
        tree: Root of the pristine kernel source.
        files: Paths relative to ``tree``.

    Returns:
        The sha256 by path; a file missing from the tree maps to ``missing``.
    """
    hashes: dict[str, str] = {}
    for name in sorted(files):
        path = tree / name
        hashes[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() else "missing"
        )
    return hashes


def check(current: dict[str, str], lock: dict[str, str]) -> list[str]:
    """Compare the tree hashes with the lock.

    Args:
        current: sha256 of the touched files in the tree.
        lock: Reference sha256 by path.

    Returns:
        One line per defect; empty when every file matches its reference.
    """
    found: list[str] = []
    for name, digest in current.items():
        if name not in lock:
            found.append(f"{name}: no reference sha256 in the lock")
        elif lock[name] != digest:
            found.append(f"{name}: sha256 changed ({lock[name]} -> {digest})")
    return found


def main(argv: list[str] | None = None) -> int:
    """Run the guard.

    Args:
        argv: Command-line arguments, ``sys.argv[1:]`` when ``None``.

    Returns:
        ``0`` when every file matches (or after ``--update``), ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("patches", nargs="+", type=Path)
    args = parser.parse_args(argv)
    tree = cast(Path, args.tree)
    lock_path = cast(Path, args.lock)
    patches = cast(list[Path], args.patches)
    files: set[str] = set()
    for patch in patches:
        files |= touched_files(patch.read_text(errors="replace"))
    current = file_hashes(tree, files)
    if cast(bool, args.update):
        merged: dict[str, str] = {}
        if lock_path.is_file():
            merged = cast(dict[str, str], json.loads(lock_path.read_text()))
        merged.update(current)
        lock_path.write_text(
            json.dumps(merged, indent=2, sort_keys=True) + "\n"
        )
        return 0
    lock = cast(dict[str, str], json.loads(lock_path.read_text()))
    defects = check(current, lock)
    for line in defects:
        print(line)
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
