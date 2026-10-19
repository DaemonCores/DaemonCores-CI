"""Unit tests for the shared ``check_*.py`` quality gates."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_build_context
import check_docs
import check_docstrings
import check_tests_manifest

GOOD_MANIFEST = {
    "boot_timeout": 60,
    "reboot_timeout": 60,
    "tests": [{
        "name": "a",
        "run": "true",
        "diag": "true"
    }],
}


def _write(path: Path, text: str) -> Path:
    """Create a file together with its parent directories.

    Args:
        path: File to create.
        text: Content to write.

    Returns:
        The created path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_docstrings_accept_a_complete_file(tmp_path: Path) -> None:
    """A fully documented file raises no defect.

    Args:
        tmp_path: Scratch directory.
    """
    source = _write(
        tmp_path / "ok.py",
        '"""Mod."""\n\n\ndef f(x: int) -> int:\n'
        '    """Double.\n\n    Args:\n        x: Value.\n\n'
        '    Returns:\n        Twice x.\n    """\n    return 2 * x\n',
    )
    assert check_docstrings.problems(source) == []


def test_docstrings_flag_missing_sections(tmp_path: Path) -> None:
    """A one-line docstring on a function with arguments is refused.

    Args:
        tmp_path: Scratch directory.
    """
    source = _write(
        tmp_path / "bad.py",
        '"""Mod."""\n\n\ndef f(x: int) -> int:\n    """Double."""\n'
        "    return 2 * x\n\n\ndef g() -> None:\n    pass\n",
    )
    found = "\n".join(check_docstrings.problems(source))
    assert "Args: section missing" in found
    assert "Returns: section missing" in found
    assert "g: docstring missing" in found


def test_docstrings_collect_defaults_to_scripts_and_tests(
    tmp_path: Path
) -> None:
    """Without a path, ``scripts`` and ``tests`` are scanned.

    Args:
        tmp_path: Scratch repository.
    """
    _write(tmp_path / "scripts/a.py", '"""A."""\n')
    _write(tmp_path / "tests/b.py", '"""B."""\n')
    _write(tmp_path / "other/c.py", '"""C."""\n')
    names = [p.name for p in check_docstrings.collect(tmp_path, [])]
    assert names == ["a.py", "b.py"]


def test_manifest_accepts_a_sound_file() -> None:
    """A complete manifest raises no defect."""
    assert check_tests_manifest.check(dict(GOOD_MANIFEST)) == []


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({
            "boot_timeout": 0
        }, "boot_timeout"),
        ({
            "tests": [{
                "name": "a",
                "run": "x"
            }]
        }, "lacks diag"),
        ({
            "tests": [{
                "name": "a",
                "run": "x",
                "diag": "y",
                "arch": "ppc"
            }]
        }, "unknown arch"),
        ({
            "tests": [{
                "name": "a",
                "run": "x",
                "diag": "y",
                "variant": "z"
            }]
        }, "unknown variant"),
        ({
            "tests": [{
                "name": "a",
                "run": "x",
                "diag": "y"
            }] * 2
        }, "duplicate name"),
    ],
)
def test_manifest_flags_defects(patch: dict[str, object], message: str) -> None:
    """Each manifest defect is reported.

    Args:
        patch: Keys overriding the sound manifest.
        message: Text expected in the report.
    """
    found = "\n".join(check_tests_manifest.check({**GOOD_MANIFEST, **patch}))
    assert message in found


def test_build_context_flags_missing_copy_and_ignored_source(
    tmp_path: Path
) -> None:
    """A missing COPY source and an ignored one are both reported.

    Args:
        tmp_path: Scratch repository.
    """
    _write(
        tmp_path / "Containerfile",
        "FROM scratch\nCOPY ./src /src\nCOPY ./gone /gone\n",
    )
    (tmp_path / "src").mkdir()
    _write(tmp_path / ".dockerignore", "# c\nsrc/\n")
    found = "\n".join(check_build_context.check_containerfiles(tmp_path))
    assert "./gone missing" in found
    assert "./src excluded by .dockerignore src" in found


def test_packages_flag_incoherent_manifest(tmp_path: Path) -> None:
    """Unknown deps, missing sources, empty builds and unhashed libs fail.

    Args:
        tmp_path: Scratch package builder directory.
    """
    _write(
        tmp_path / "packages.yml",
        "packages:\n"
        "  - name: a\n    deps: [ghost]\n    sources: [nope]\n"
        "    build: |\n      . lib/x.sh\n"
        "  - name: b\n",
    )
    found = "\n".join(check_build_context.check_packages(tmp_path))
    assert "a depends on unknown ghost" in found
    assert "a: nope missing" in found
    assert "a: lib/x.sh used by build but not in sources" in found
    assert "b: neither upstream nor build" in found


def test_docs_flag_stale_citations_and_links(tmp_path: Path) -> None:
    """Citations past the file end or on blank lines and dead links fail.

    Args:
        tmp_path: Scratch repository.
    """
    _write(tmp_path / "code.txt", "one\n\nthree\n")
    _write(
        tmp_path / "docs/page.md",
        "ok [src: file: code.txt:1]\n"
        "blank [src: file: code.txt:2]\n"
        "far [src: file: code.txt:9]\n"
        "gone [src: file: nothing.txt:1]\n"
        "[dead](missing.md) [live](page.md) [web](https://x.y)\n",
    )
    found = "\n".join(
        check_docs.check_page(tmp_path, tmp_path / "docs/page.md")
    )
    assert "code.txt:2 is a blank line" in found
    assert "code.txt:9 is past the end" in found
    assert "nothing.txt does not exist" in found
    assert "broken link missing.md" in found
    assert "code.txt:1" not in found
    assert "page.md" not in found.replace("docs/page.md", "")


def test_docs_skip_kronn_backups(tmp_path: Path) -> None:
    """Pages under the Kronn backup directory are not verified.

    Args:
        tmp_path: Scratch repository.
    """
    _write(tmp_path / "docs/a.md", "x\n")
    _write(tmp_path / "docs/.kronn-citation-originals/b.md", "x\n")
    names = [p.name for p in check_docs.pages(tmp_path / "docs")]
    assert names == ["a.md"]
