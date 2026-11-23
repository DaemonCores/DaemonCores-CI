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


def wave(name: str, deps: dict[str, list[str]], memo: dict[str, int]) -> int:
    """Measure the wave of one package, memoised.

    Args:
        name: Package name to measure.
        deps: Direct dependencies of every package.
        memo: Waves already measured, filled as a side effect.

    Returns:
        The wave index: one more than the deepest dep.
    """
    if name in memo:
        return memo[name]
    d = deps.get(name, [])
    memo[name] = 0 if not d else 1 + max(wave(x, deps, memo) for x in d)
    return memo[name]


def main(path: str) -> None:
    """Read the manifest JSON at ``path`` and append the wave outputs.

    Args:
        path: Manifest JSON file listing the packages and their deps.
    """
    with open(path, encoding="utf-8") as handle:
        pkgs = json.load(handle)["packages"] or []
    deps = {p["name"]: (p.get("deps") or []) for p in pkgs}
    memo: dict[str, int] = {}

    waves: dict[int, list[str]] = {}
    for name in deps:
        waves.setdefault(wave(name, deps, memo), []).append(name)
    # The workflow has one matrix job per wave: a deeper package would be
    # neither built nor skipped, and the carry-over would keep it stale.
    too_deep = sorted(
        n for i, names in waves.items() if i >= WAVE_COUNT for n in names
    )
    if too_deep:
        sys.exit(
            f"dependency chain deeper than {WAVE_COUNT} waves, "
            f"add a wave job for: {', '.join(too_deep)}"
        )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        for i in range(WAVE_COUNT):
            out.write(f"wave{i}={json.dumps(sorted(waves.get(i, [])))}\n")


if __name__ == "__main__":
    main(sys.argv[1])
