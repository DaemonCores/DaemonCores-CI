"""check_tests_manifest.py - static checks of ``workflows/tests.yml``.

Usage:
    python3 check_tests_manifest.py [--root DIR] [--manifest FILE]

The boot harness (``image-test/image-test.sh``) refuses to start the VM when a
test lacks ``name``, ``run`` or ``diag``. This check surfaces the same
manifest errors in well under a second instead of after a build.
"""

import argparse
import sys
from pathlib import Path
from typing import cast

import yaml

SECTIONS = ("tests", "reboot_tests")
ARCHES = {"amd64", "arm64"}


def check(data: dict[str, object]) -> list[str]:
    """Validate a parsed manifest.

    Args:
        data: The manifest as a mapping.

    Returns:
        One line per defect found; empty when the manifest is sound.
    """
    found: list[str] = []
    if not data.get("tests"):
        found.append("no tests declared")
    for key in ("boot_timeout", "reboot_timeout"):
        value = data.get(key)
        if not isinstance(value, int) or value <= 0:
            found.append(f"{key} must be a positive integer")
    for section in SECTIONS:
        names: list[str] = []
        for i, raw in enumerate(
            cast(list[dict[str, object]],
                 data.get(section) or [])
        ):
            entry = raw
            where = f"{section}[{i}]"
            for key in ("name", "run", "diag"):
                value = entry.get(key)
                if not isinstance(value, str) or not value.strip():
                    found.append(f"{where} lacks {key}")
            if entry.get("arch", "amd64") not in ARCHES:
                found.append(f"{where} has an unknown arch")
            names.append(str(entry.get("name")))
        if len(names) != len(set(names)):
            found.append(f"duplicate name in {section}")
    return found


class Options(argparse.Namespace):
    """Typed command-line options.

    Attributes:
        root: Repository to inspect.
        manifest: Manifest path, relative to the root.
    """

    root: Path
    manifest: str


def main() -> int:
    """Entry point: print every defect and fail when there is one.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", default="workflows/tests.yml")
    args = parser.parse_args(namespace=Options())
    path = args.root.resolve() / args.manifest
    if not path.is_file():
        print(f"{path}: manifest not found", file=sys.stderr)
        return 1
    found = check(cast(dict[str, object], yaml.safe_load(path.read_text())))
    for line in found:
        print(f"{args.manifest}: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
