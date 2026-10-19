"""Unit tests for ``scripts/check_kconfig.py``."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_kconfig


@pytest.fixture(name="tree")
def fixture_tree(tmp_path: Path) -> Path:
    """Build a tiny kernel tree declaring two symbols.

    Args:
        tmp_path: Pytest scratch directory.

    Returns:
        The tree root.
    """
    (tmp_path / "drivers").mkdir()
    (tmp_path / "drivers" / "Kconfig").write_text(
        "menuconfig FOO\n\tbool \"foo\"\n\nconfig BAR\n\ttristate \"bar\"\n"
    )
    return tmp_path


def _fragment(tmp_path: Path, text: str) -> Path:
    """Write a config fragment.

    Args:
        tmp_path: Directory receiving the file.
        text: Fragment content.

    Returns:
        The fragment path.
    """
    path = tmp_path / "fragment"
    path.write_text(text)
    return path


def test_parse_fragment_reads_values_and_unset(tmp_path: Path) -> None:
    """Set and unset lines are read and the last value wins.

    Args:
        tmp_path: Pytest scratch directory.
    """
    path = _fragment(
        tmp_path, "# a comment\nCONFIG_A=y\nCONFIG_A=m\n"
        "# CONFIG_B is not set\nCONFIG_C=\"x\"\n"
    )
    assert check_kconfig.parse_fragment(path) == {
        "CONFIG_A": "m",
        "CONFIG_B": "n",
        "CONFIG_C": "\"x\"",
    }


def test_unknown_symbol_is_reported(tree: Path, tmp_path: Path) -> None:
    """A symbol the tree does not declare fails, a declared one passes.

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=y\nCONFIG_GONE=m\n")
    code = check_kconfig.main(["--tree", str(tree), str(fragment)])
    assert code == 1
    assert check_kconfig.check({"CONFIG_FOO": "y"},
                               check_kconfig.declared_symbols(tree), None) == []


def test_dropped_option_is_reported(tree: Path, tmp_path: Path) -> None:
    """An option the generated .config lost to a dependency fails.

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=y\nCONFIG_BAR=m\n")
    config = tmp_path / ".config"
    config.write_text("CONFIG_FOO=y\n# CONFIG_BAR is not set\n")
    defects = check_kconfig.check(
        check_kconfig.parse_fragment(fragment),
        check_kconfig.declared_symbols(tree),
        check_kconfig.config_values(config)
    )
    assert defects == [
        "CONFIG_BAR: wanted m, .config has n (unmet dependency?)"
    ]


def test_honoured_options_pass(tree: Path, tmp_path: Path) -> None:
    """A fragment fully honoured by the .config exits 0.

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=y\n# CONFIG_BAR is not set\n")
    config = tmp_path / ".config"
    config.write_text("CONFIG_FOO=y\n# CONFIG_BAR is not set\n")
    args = ["--tree", str(tree), "--config", str(config), str(fragment)]
    assert check_kconfig.main(args) == 0
