#!/usr/bin/env python3
"""Print the build order of the packages of an expanded manifest.

The order is the dependency level first (a package comes after everything it
depends on), then kernel-compiling packages before the rest of their level:
they are the slowest builds and share one compiler cache, so running them
first keeps that cache hot. A package is a kernel build when its build script
calls ``kernel_signing_setup`` (the marker pkg-def.sh uses for the same
purpose); among them, the ones with no dependency (the kernels themselves)
precede the ones that depend on a kernel (the module packages).

Usage:
    plan.py <expanded-manifest.json>      # one package name per line
"""

import json
import sys
from typing import NotRequired, TypedDict, cast

KERNEL_MARKER = 'kernel_signing_setup'


class PackageEntry(TypedDict):
    """One package entry of an expanded manifest.

    Attributes:
        name: Package name.
        deps: Names of the packages it depends on.
        build: Build script text of the package.
    """

    name: str
    deps: NotRequired[list[str]]
    build: NotRequired[str]


def build_order(packages: list[PackageEntry]) -> list[str]:
    """Return the package names in build order.

    Args:
        packages: the ``packages`` list of the expanded manifest.

    Returns:
        Names ordered by (dependency level, kernel group, name).
    """
    deps = {p['name']: list(p.get('deps') or []) for p in packages}
    signs = {
        p['name']: KERNEL_MARKER in (p.get('build') or '')
        for p in packages
    }
    memo: dict[str, int] = {}

    def level(name: str) -> int:
        if name not in memo:
            memo[name] = 1 + max((level(d) for d in deps.get(name, [])),
                                 default=-1)
        return memo[name]

    def group(name: str) -> int:
        if not signs[name]:
            return 2
        return 0 if not deps[name] else 1

    return sorted(deps, key=lambda n: (level(n), group(n), n))


def main(argv: list[str]) -> int:
    """Read the manifest JSON named by argv[1] and print the order.

    Args:
        argv: Command-line arguments; argv[1] is the manifest path.

    Returns:
        Process exit code: 0 on success, 2 on a usage error.
    """
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding='utf-8') as handle:
        packages = cast("list[PackageEntry]", json.load(handle)['packages'])
    print('\n'.join(build_order(packages)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
