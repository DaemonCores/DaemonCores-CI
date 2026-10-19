"""check_kconfig.py - Kconfig symbol gate for kernel config fragments.

Usage:
    python3 check_kconfig.py --tree DIR [--config FILE] FRAGMENT [FRAGMENT ...]

``make olddefconfig`` silently ignores a symbol the kernel does not know, so a
fragment that names a renamed or removed option builds a kernel without it and
nothing fails. This gate closes the gap against the kernel the build uses:

* every symbol of every fragment must be declared by a Kconfig file of the
  kernel source ``--tree``;
* with ``--config`` (the ``.config`` produced by ``make olddefconfig``), every
  symbol must also end up with the value the fragments ask for, which catches
  an option dropped because a dependency is not met.

The last fragment wins, as with ``merge_config.sh``.
"""

import argparse
import re
import sys
from pathlib import Path

SET = re.compile(r"^(CONFIG_\w+)=(.*)$")
UNSET = re.compile(r"^# (CONFIG_\w+) is not set$")
DECLARED = re.compile(r"^\s*(?:menu)?config\s+(\w+)\s*$")


def parse_fragment(path: Path) -> dict[str, str]:
    """Read the requested values of one fragment.

    Args:
        path: Config fragment (``CONFIG_X=y`` or ``# CONFIG_X is not set``).

    Returns:
        The requested value by symbol, ``n`` for a symbol that must be unset.
    """
    wanted: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if match := SET.match(line):
            wanted[match.group(1)] = match.group(2)
        elif match := UNSET.match(line):
            wanted[match.group(1)] = "n"
    return wanted


def declared_symbols(tree: Path) -> set[str]:
    """Collect every symbol declared by a Kconfig file of a kernel tree.

    Args:
        tree: Root of the kernel source.

    Returns:
        The symbols as ``CONFIG_<name>``.
    """
    found: set[str] = set()
    for kconfig in tree.rglob("Kconfig*"):
        if not kconfig.is_file():
            continue
        for line in kconfig.read_text(errors="replace").splitlines():
            if match := DECLARED.match(line):
                found.add(f"CONFIG_{match.group(1)}")
    return found


def config_values(path: Path) -> dict[str, str]:
    """Read the final values of a generated ``.config``.

    Args:
        path: The ``.config`` written by ``make olddefconfig``.

    Returns:
        The value by symbol, ``n`` for a symbol that is not set.
    """
    return parse_fragment(path)


def check(
    wanted: dict[str, str],
    declared: set[str],
    final: dict[str, str] | None,
) -> list[str]:
    """Compare the requested options with the kernel.

    Args:
        wanted: Requested value by symbol, all fragments merged.
        declared: Symbols the kernel Kconfig files declare.
        final: Values of the generated ``.config``, or ``None`` to skip that
            comparison.

    Returns:
        One line per defect; empty when every option is honoured.
    """
    found: list[str] = []
    for symbol, value in sorted(wanted.items()):
        if symbol not in declared:
            found.append(f"{symbol}: not declared by this kernel")
        elif final is not None and final.get(symbol, "n") != value:
            found.append(
                f"{symbol}: wanted {value}, .config has "
                f"{final.get(symbol, 'n')} (unmet dependency?)"
            )
    return found


def main(argv: list[str] | None = None) -> int:
    """Run the gate.

    Args:
        argv: Command-line arguments, ``sys.argv[1:]`` when ``None``.

    Returns:
        ``0`` when every option is honoured, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("fragments", nargs="+", type=Path)
    args = parser.parse_args(argv)
    wanted: dict[str, str] = {}
    for fragment in args.fragments:
        wanted.update(parse_fragment(fragment))
    final = config_values(args.config) if args.config else None
    defects = check(wanted, declared_symbols(args.tree), final)
    for line in defects:
        print(line)
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
