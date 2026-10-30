"""Unit tests for ``scripts/gen_docs.py``."""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gen_docs


def test_shell_header_extracts_comment_block(tmp_path: Path) -> None:
    """The header is the comment block right after the shebang.

    Args:
        tmp_path: Scratch directory holding the sample script.
    """
    script = tmp_path / "s.sh"
    script.write_text(
        "#!/bin/bash\n#\n# name - purpose\n# more\nset -e\n# x\n",
        encoding="utf-8"
    )
    assert gen_docs._shell_header(script) == "name - purpose\nmore"


def test_page_replaces_only_generated_region() -> None:
    """Text outside the markers survives regeneration."""
    current = f"intro\n{gen_docs.START}\nold\n{gen_docs.END}\noutro\n"
    new = gen_docs._page("T", "new", current)
    assert new.startswith("intro\n")
    assert new.endswith("\noutro\n")
    assert "old" not in new
    assert "new" in new


def test_root_option_documents_another_repository(tmp_path: Path) -> None:
    """``--root`` reads the Makefile lists of the given repository.

    Args:
        tmp_path: Scratch repository with a Makefile and one script.
    """
    (
        tmp_path / "Makefile"
    ).write_text("PYTHON_SRCS := m.py\nSHELL_SRCS := s.sh\nPOSTINST_SRCS :=\n")
    (tmp_path / "m.py").write_text('"""Module doc."""\n')
    (tmp_path /
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
    (tmp_path / "Makefile").write_text(
        "PYTHON_SRCS := m.py\nSHELL_SRCS :=\nPOSTINST_SRCS :=\n"
    )
    (tmp_path / "m.py").write_text('"""Module doc."""\n')
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
