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
from typing import cast

SET = re.compile(r"^(CONFIG_\w+)=(.*)$")
UNSET = re.compile(r"^# (CONFIG_\w+) is not set$")
DECLARED = re.compile(r"^\s*(?:menu)?config\s+(\w+)\s*$")
PROMPT = re.compile(r"^\s*(?:bool|tristate|string|hex|int)\s*[\"'].+?")


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
    return _scan_kconfigs(tree)[0]


def prompted_symbols(tree: Path) -> set[str]:
    """Symbols a user can actually set (they carry a prompt)."""
    return _scan_kconfigs(tree)[1]


def _scan_kconfigs(tree: Path) -> tuple[set[str], set[str]]:
    """Declared symbols and prompted symbols of a kernel tree.

    Args:
        tree: Root of the kernel source.

    Returns:
        Declared symbols as ``CONFIG_<name>`` and the subset that carries a
        user prompt (anything promptless is select-managed: a fragment ``=``
        line is advisory, ``olddefconfig`` ignores it).
    """
    """Collect every symbol declared by a Kconfig file of a kernel tree.

    Args:
        tree: Root of the kernel source.

    Returns:
        The symbols as ``CONFIG_<name>``.
    """
    declared: set[str] = set()
    prompted: set[str] = set()
    current: str | None = None
    prompted_current = False
    for kconfig in tree.rglob("Kconfig*"):
        if not kconfig.is_file():
            continue
        for line in kconfig.read_text(errors="replace").splitlines():
            if match := DECLARED.match(line):
                if current and prompted_current:
                    prompted.add(f"CONFIG_{current}")
                current = match.group(1)
                prompted_current = False
                declared.add(f"CONFIG_{current}")
            elif current is not None and PROMPT.match(line):
                prompted_current = True
    if current and prompted_current:
        prompted.add(f"CONFIG_{current}")
    return declared, prompted


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
    prompts: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Compare the requested options with the kernel.

    Args:
        wanted: Requested value by symbol, all fragments merged.
        declared: Symbols the kernel Kconfig files declare.
        final: Values of the generated ``.config``, or ``None`` to skip that
            comparison.

    Returns:
        Two lists: one defect line per real problem (build fails) and one
        advisory line per ignored case (build passes, logged loud).

    Advisory cases (the FULL SERVER union era, owner 11/11: the base carries
    the Debian/Proxmox distro configs of neighbouring kernel versions):
    * ``not declared by this kernel`` — the driver was removed/renamed between
      the reference distro config and this tree; ``olddefconfig`` drops it.
    * an invisible, promptless helper whose value ``olddefconfig`` resolved
      through its selects/parents (its own ``=`` line is advisory).

    A tristate superset passes too: a requested ``=m`` honoured as ``=y``
    (built-in includes the module). A promptless symbol (select-managed) is
    advisory wherever its line is dropped or re-valued: ``olddefconfig``
    handles it through the parent.
    """
    defects: list[str] = []
    advisory: list[str] = []
    for symbol, value in sorted(wanted.items()):
        if symbol not in declared:
            advisory.append(f"WARN: {symbol}: not declared by this kernel (dropped)")
        elif final is not None:
            if symbol not in final:
                advisory.append(
                    f"WARN: {symbol}: not in .config (invisible in this "
                    "scope/architecture — dropped)"
                )
            elif prompts is not None and symbol not in prompts:
                advisory.append(
                    f"WARN: {symbol}: promptless, resolved by olddefconfig "
                    f"({final[symbol]})"
                )
            else:
                has = final[symbol]
                if value == "m" and has == "y":
                    advisory.append(
                        f"WARN: {symbol}: wanted m, built-in =y (covers m)"
                    )
                elif has != value:
                    defects.append(
                        f"{symbol}: wanted {value}, .config has "
                        f"{has} (unmet dependency?)"
                    )
    return defects, advisory


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
    tree = cast(Path, args.tree)
    config = cast(Path | None, args.config)
    fragments = cast(list[Path], args.fragments)
    wanted: dict[str, str] = {}
    for fragment in fragments:
        wanted.update(parse_fragment(fragment))
    final = config_values(config) if config else None
    declared, prompts = _scan_kconfigs(tree)
    defects, advisory = check(wanted, declared, final, prompts)
    for line in advisory:
        print(line)
    for line in defects:
        print(line)
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main())
