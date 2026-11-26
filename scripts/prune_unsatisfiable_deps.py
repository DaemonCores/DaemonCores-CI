"""Strip deps the dispatch-only filter dropped from an expanded manifest.

Usage: prune-unsatisfiable-deps.py <manifest.json>

A dep dropped by the only= filter is no longer in the manifest: it cannot appear
in any wave and its deb never lands in /tmp/prior. Keeping the dep reference
would fail every build of that package ("Install dependency debs" resolves
nothing). Removing it lets the package run with its OWN fallback source (e.g.
zfs-module-pve consumes the published APT index of a previous green run on a
standalone dispatch). The unfiltered path (normal push) is untouched: every dep
is then in the manifest and nothing is stripped.
"""

import json
import sys
from typing import NotRequired, TypedDict, cast


class PackageEntry(TypedDict):
    """One package entry of an expanded manifest.

    Attributes:
        name: Package name.
        deps: Names of the packages it depends on.
    """

    name: str
    deps: NotRequired[list[str]]


def main(path: str) -> None:
    """Remove deps absent from the manifest at ``path``, rewriting it.

    Args:
        path: Manifest JSON file, rewritten in place.
    """
    with open(path, encoding="utf-8") as fh:
        m = cast("dict[str, object]", json.load(fh))
    packages = cast("list[PackageEntry]", m["packages"])
    names = {p["name"] for p in packages}
    for p in packages:
        deps = p.get("deps") or []
        kept = [d for d in deps if d in names]
        if kept != deps:
            dropped = [d for d in deps if d not in names]
            print(
                "only filter: dropped unsatisfiable deps of " +
                f"{p['name']}: {dropped}",
                file=sys.stderr
            )
            p["deps"] = kept
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(m, fh)


if __name__ == "__main__":
    main(sys.argv[1])
