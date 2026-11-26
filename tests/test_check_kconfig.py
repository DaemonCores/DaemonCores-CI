"""Unit tests for ``scripts/check_kconfig.py``."""

import runpy
import sys
import textwrap
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
    _ = (tmp_path / "drivers" / "Kconfig").write_text(
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
    _ = path.write_text(text)
    return path


def test_parse_fragment_reads_values_and_unset(tmp_path: Path) -> None:
    """Set and unset lines are read and the last value wins.

    Args:
        tmp_path: Pytest scratch directory.
    """
    quoted = '"x"'
    path = _fragment(
        tmp_path,
        textwrap.dedent(
            f"""\
            # a comment
            CONFIG_A=y
            CONFIG_A=m
            # CONFIG_B is not set
            CONFIG_C={quoted}
            """
        ),
    )
    assert check_kconfig.parse_fragment(path) == {
        "CONFIG_A": "m",
        "CONFIG_B": "n",
        "CONFIG_C": "\"x\"",
    }


def test_unknown_symbol_is_reported(tree: Path, tmp_path: Path) -> None:
    """Report an undeclared symbol as advisory, a declared one silently.

    A symbol the tree does not declare is an ADVISORY (the FULL SERVER
    union era: a distro config of a neighbouring kernel version drops
    removed drivers — the build passes, the gate logs loud), a declared one
    passes silent.

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=y\nCONFIG_GONE=m\n")
    code = check_kconfig.main(["--tree", str(tree), str(fragment)])
    assert code == 0
    defects, advisory = check_kconfig.check(
        {
            "CONFIG_FOO": "y",
            "CONFIG_GONE": "m"
        },
        check_kconfig.declared_symbols(tree),
        None,
        check_kconfig.prompted_symbols(tree),
    )
    assert defects == []
    assert any("CONFIG_GONE" in line for line in advisory)


def test_dropped_option_is_reported(tree: Path, tmp_path: Path) -> None:
    """Fail an option the generated .config lost to a dependency.

    It is a real defect (the prompt is owned, not promptless).

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=y\nCONFIG_BAR=m\n")
    config = tmp_path / ".config"
    _ = config.write_text("CONFIG_FOO=y\n# CONFIG_BAR is not set\n")
    declared, prompts = check_kconfig.declared_symbols(tree), \
        check_kconfig.prompted_symbols(tree)
    defects, advisory = check_kconfig.check(
        check_kconfig.parse_fragment(fragment),
        declared,
        check_kconfig.config_values(config),
        prompts,
    )
    assert defects == [
        "CONFIG_BAR: wanted m, .config has n (unmet dependency?)"
    ]
    assert advisory == []


def test_honoured_options_pass(tree: Path, tmp_path: Path) -> None:
    """A fragment fully honoured by the .config exits 0.

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=y\n# CONFIG_BAR is not set\n")
    config = tmp_path / ".config"
    _ = config.write_text("CONFIG_FOO=y\n# CONFIG_BAR is not set\n")
    args = ["--tree", str(tree), "--config", str(config), str(fragment)]
    assert check_kconfig.main(args) == 0


def test_scan_skips_a_directory_named_like_a_kconfig(tmp_path: Path) -> None:
    """A non-file Kconfig entry is skipped and a trailing prompt is absent.

    Args:
        tmp_path: Scratch kernel tree.
    """
    (tmp_path / "Kconfig.dir").mkdir()
    _ = (tmp_path / "Kconfig").write_text("config QUIET\n\tbool\n")
    assert check_kconfig.declared_symbols(tmp_path) == {"CONFIG_QUIET"}
    assert check_kconfig.prompted_symbols(tmp_path) == set()


def test_check_warns_when_a_symbol_is_missing_from_the_config() -> None:
    """A declared symbol absent from .config is advisory, not a defect."""
    defects, advisory = check_kconfig.check({"CONFIG_FOO": "y"}, {"CONFIG_FOO"},
                                            {}, {"CONFIG_FOO"})
    assert defects == []
    assert any("not in .config" in line for line in advisory)


def test_check_warns_when_a_promptless_symbol_was_resolved() -> None:
    """A symbol with no prompt is advisory: olddefconfig owns it."""
    defects, advisory = check_kconfig.check({"CONFIG_FOO": "y"}, {"CONFIG_FOO"},
                                            {"CONFIG_FOO": "y"}, set())
    assert defects == []
    assert any("promptless" in line and "(y)" in line for line in advisory)


def test_check_accepts_a_module_request_honoured_as_built_in() -> None:
    """A requested module built in is an advisory: y covers m."""
    defects, advisory = check_kconfig.check({"CONFIG_FOO": "m"}, {"CONFIG_FOO"},
                                            {"CONFIG_FOO": "y"}, {"CONFIG_FOO"})
    assert defects == []
    assert any("built-in =y" in line for line in advisory)


def test_cli_prints_a_defect_and_exits_1(
    tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value the .config does not honour fails the gate and is printed.

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
        capsys: Output capture.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=m\n")
    config = tmp_path / ".config"
    _ = config.write_text("CONFIG_FOO=n\n")
    code = check_kconfig.main([
        "--tree", str(tree), "--config",
        str(config),
        str(fragment)
    ])
    assert code == 1
    assert "CONFIG_FOO: wanted m, .config has n" in capsys.readouterr().out


def test_running_the_gate_as_main_exits_with_its_status(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executed as a script, the gate exits 0 on a honoured fragment.

    Args:
        tree: Kernel tree fixture.
        tmp_path: Pytest scratch directory.
        monkeypatch: Environment patcher.
    """
    fragment = _fragment(tmp_path, "CONFIG_FOO=y\n")
    monkeypatch.setattr(
        sys, "argv",
        ["check_kconfig.py", "--tree",
         str(tree), str(fragment)]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(
                Path(__file__).resolve().parents[1] / "scripts" /
                "check_kconfig.py"
            ),
            run_name="__main__"
        )
    assert excinfo.value.code == 0
