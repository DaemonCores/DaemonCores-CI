"""Strip deps the dispatch-only filter dropped from an expanded manifest.

Usage: prune-unsatisfiable-deps.py <manifest.json>

A dep dropped by the only= filter is no longer in the manifest: it cannot
appear in any wave and its deb never lands in /tmp/prior. Keeping the dep
reference would fail every build of that package ("Install dependency debs"
resolves nothing). Removing it lets the package run with its OWN fallback
source (e.g. zfs-module-pve consumes the published APT index of a previous
green run on a standalone dispatch). The unfiltered path (normal push) is
untouched: every dep is then in the manifest and nothing is stripped.
"""
import json
import sys


def main(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        m = json.load(fh)
    names = {p["name"] for p in m["packages"]}
    for p in m["packages"]:
        deps = p.get("deps") or []
        kept = [d for d in deps if d in names]
        if kept != deps:
            print(
                "only filter: dropped unsatisfiable deps of %s: %s" %
                (p["name"], [d for d in deps if d not in names]),
                file=sys.stderr
            )
            p["deps"] = kept
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(m, fh)


if __name__ == "__main__":
    main(sys.argv[1])
