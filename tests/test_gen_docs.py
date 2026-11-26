"""Unit tests for ``scripts/gen_docs.py``."""

import io
import runpy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gen_docs

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_shell_header_extracts_comment_block(tmp_path: Path) -> None:
    """The header is the comment block right after the shebang.

    Args:
        tmp_path: Scratch directory holding the sample script.
    """
    script = tmp_path / "s.sh"
    _ = script.write_text(
        "#!/bin/bash\n#\n# name - purpose\n# more\nset -e\n# x\n",
        encoding="utf-8"
    )
    assert gen_docs.shell_header(script) == "name - purpose\nmore"


def test_page_replaces_only_generated_region() -> None:
    """Text outside the markers survives regeneration."""
    current = f"intro\n{gen_docs.START}\nold\n{gen_docs.END}\noutro\n"
    new = gen_docs.page("T", "new", current)
    assert new.startswith("intro\n")
    assert new.endswith("\noutro\n")
    assert "old" not in new
    assert "new" in new


def test_root_option_documents_another_repository(tmp_path: Path) -> None:
    """``--root`` reads the Makefile lists of the given repository.

    Args:
        tmp_path: Scratch repository with a Makefile and one script.
    """
    _ = (
        tmp_path / "Makefile"
    ).write_text("PYTHON_SRCS := m.py\nSHELL_SRCS := s.sh\nPOSTINST_SRCS :=\n")
    _ = (tmp_path / "m.py").write_text('"""Module doc."""\n')
    _ = (tmp_path /
         "s.sh").write_text("#!/bin/bash\n#\n# s - does things\nset -e\n")
    sys.argv = ["gen_docs.py", "--root", str(tmp_path)]
    assert gen_docs.main() == 0
    assert (tmp_path / "docs/reference/Reference-Python.md").is_file()
    sys.argv = ["gen_docs.py", "--root", str(tmp_path), "--check"]
    assert gen_docs.main() == 0


def test_dangling_docs_symlink_skips_the_reference_check(
    tmp_path: Path,
) -> None:
    """A docs -> wiki symlink with no wiki clone in CI is skipped, not stale.

    Args:
        tmp_path: Scratch repository with a docs symlink pointing nowhere.
    """
    _ = (tmp_path / "Makefile"
         ).write_text("PYTHON_SRCS := m.py\nSHELL_SRCS :=\nPOSTINST_SRCS :=\n")
    _ = (tmp_path / "m.py").write_text('"""Module doc."""\n')
    (tmp_path / "docs").symlink_to(tmp_path / "absent-wiki")
    sys.argv = ["gen_docs.py", "--root", str(tmp_path), "--check"]
    errs = io.StringIO()
    old = sys.stderr
    sys.stderr = errs
    try:
        assert gen_docs.main() == 0
    finally:
        sys.stderr = old
    assert "reference check skipped" in errs.getvalue()


def test_a_makefile_without_the_shell_variables_documents_no_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing SHELL_SRCS and POSTINST_SRCS lists yield an empty shell page.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
    """
    _ = (tmp_path / "Makefile").write_text("PYTHON_SRCS :=\n")
    monkeypatch.setattr(sys, "argv", ["gen_docs.py", "--root", str(tmp_path)])
    assert gen_docs.main() == 0
    shell_page = (tmp_path / "docs/reference/Reference-Shell.md").read_text()
    assert "## `" not in shell_page


def test_python_page_marks_undocumented_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module and a function without docstring are marked, not dropped.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
    """
    _ = (tmp_path / "Makefile").write_text("PYTHON_SRCS := bare.py\n")
    _ = (tmp_path / "bare.py").write_text("def f(x):\n    return x\n")
    monkeypatch.setattr(sys, "argv", ["gen_docs.py", "--root", str(tmp_path)])
    assert gen_docs.main() == 0
    page = (tmp_path / "docs/reference/Reference-Python.md").read_text()
    assert "(no module docstring)" in page
    assert "### `f()`" in page
    assert "(no docstring)" in page


def test_shell_header_reads_a_script_that_is_all_comments(
    tmp_path: Path
) -> None:
    """A script ending in its comment block yields every line.

    Args:
        tmp_path: Scratch directory holding the sample script.
    """
    script = tmp_path / "s.sh"
    _ = script.write_text("#!/bin/sh\n# one\n#\n# two\n")
    assert gen_docs.shell_header(script) == "one\n\ntwo"


def test_shell_sources_from_the_makefile_are_documented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SHELL_SRCS path renders the script header in the shell reference.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
    """
    _ = (tmp_path / "Makefile").write_text(
        "PYTHON_SRCS :=\nSHELL_SRCS := scripts/s.sh\nPOSTINST_SRCS :=\n"
    )
    (tmp_path / "scripts").mkdir()
    _ = (
        tmp_path / "scripts"
    ).joinpath("s.sh").write_text("#!/bin/bash\n# s - does things\nset -e\n")
    monkeypatch.setattr(sys, "argv", ["gen_docs.py", "--root", str(tmp_path)])
    assert gen_docs.main() == 0
    page = (tmp_path / "docs/reference/Reference-Shell.md").read_text()
    assert "scripts/s.sh" in page
    assert "s - does things" in page


def test_check_mode_fails_and_writes_nothing_when_pages_are_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """--check on a tree with no generated page exits 1 without writing.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
        capsys: Output capture.
    """
    _ = (tmp_path / "Makefile"
         ).write_text("PYTHON_SRCS :=\nSHELL_SRCS :=\nPOSTINST_SRCS :=\n")
    monkeypatch.setattr(
        sys, "argv", ["gen_docs.py", "--root",
                      str(tmp_path), "--check"]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(str(SCRIPTS / "gen_docs.py"), run_name="__main__")
    assert excinfo.value.code == 1
    assert "out of date" in capsys.readouterr().err
    assert not (tmp_path / "docs/reference/Reference-Python.md").exists()
