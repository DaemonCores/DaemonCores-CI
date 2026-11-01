"""Compute dependency waves from a resolved manifest converted to JSON.

Usage: dependency-waves.py <packages.json>

A package with no deps is wave 0; otherwise one more than its deepest dep.
Writes ``wave0..wave3=<JSON array of package names>`` to the file named by
``$GITHUB_OUTPUT``.
"""
import json
import os
import sys

WAVE_COUNT = 4


def main(path: str) -> None:
    """Read the manifest JSON at ``path`` and append the wave outputs."""
    with open(path, encoding="utf-8") as handle:
        pkgs = json.load(handle)["packages"]
    deps = {p["name"]: (p.get("deps") or []) for p in pkgs}
    memo: dict[str, int] = {}

    def wave(name: str) -> int:
        if name in memo:
            return memo[name]
        d = deps.get(name, [])
        memo[name] = 0 if not d else 1 + max(wave(x) for x in d)
        return memo[name]

    waves: dict[int, list[str]] = {}
    for name in deps:
        waves.setdefault(wave(name), []).append(name)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        for i in range(WAVE_COUNT):
            out.write("wave%d=%s\n" % (i, json.dumps(sorted(waves.get(i, [])))))


if __name__ == "__main__":
    main(sys.argv[1])
