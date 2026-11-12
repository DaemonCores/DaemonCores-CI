"""Keep the transitive dependency closure of a dispatch-only filter.

Usage: only-deps-closure.py <expanded_manifest.json> <only_name>

The `only=` filter keeps the packages matching <only_name>; this script adds
their TRANSITIVE deps back to the kept set, so a scoped cycle still builds
(and content-cache-restore is nearly free) every package the consumer needs:
a gpu/modules entry consumes its profile kernel's Module.symvers, a dep
dropped by the plain filter dies on an empty /tmp/prior pool (the
"Unsupported file /tmp/prior/<name>_*_*.deb" apt failure of run 43224452266).
The entry itself is untouched: the closure only ADDS names already in the
expanded manifest, and the pruner (prune_unsatisfiable_deps.py) still runs
afterwards as the standalone-family safety net.
"""
import collections
import json
import sys


def main(path: str, only: str) -> None:
    with open(path, encoding="utf-8") as fh:
        m = json.load(fh)
    pkgs = m.get("packages", [])
    info = {
        p["name"]: p for p in pkgs if isinstance(p, dict) and "name" in p
    }
    keep = {
        name for name in info
        if name == only or name.startswith(only + "-")
    }
    frontier = collections.deque(keep)
    while frontier:
        name = frontier.popleft()
        for dep in info.get(name, {}).get("deps") or []:
            if dep in info and dep not in keep:
                keep.add(dep)
                frontier.append(dep)
    dropped = len(info) - len(keep)
    m["packages"] = [p for p in pkgs if p.get("name") in keep]
    print(
        "only closure: kept %d/%d packages (%d dropped): %s"
        % (len(keep), len(info), dropped, " ".join(sorted(keep))),
        file=sys.stderr,
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(m, fh)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
