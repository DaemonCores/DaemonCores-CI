"""Unit tests for the shared ``check_*.py`` quality gates."""

import runpy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import textwrap

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

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
MANIFEST_YAML = (
    "boot_timeout: 60\n"
    "reboot_timeout: 60\n"
    "tests:\n"
    "  - name: a\n"
    '    run: "true"\n'
    '    diag: "true"\n'
)


def _write(path: Path, text: str) -> Path:
    """Create a file together with its parent directories.

    Args:
        path: File to create.
        text: Content to write.

    Returns:
        The created path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text, encoding="utf-8")
    return path


def test_docstrings_accept_a_complete_file(tmp_path: Path) -> None:
    """A fully documented file raises no defect.

    Args:
        tmp_path: Scratch directory.
    """
    source = _write(
        tmp_path / "ok.py",
        textwrap.dedent(
            '''\
            """Mod."""


            def f(x: int) -> int:
                """Double.

                Args:
                    x: Value.

                Returns:
                    Twice x.
                """
                return 2 * x
            '''
        ),
    )
    assert check_docstrings.problems(source) == []


def test_docstrings_flag_missing_sections(tmp_path: Path) -> None:
    """A one-line docstring on a function with arguments is refused.

    Args:
        tmp_path: Scratch directory.
    """
    source = _write(
        tmp_path / "bad.py",
        textwrap.dedent(
            '''\
            """Mod."""


            def f(x: int) -> int:
                """Double."""
                return 2 * x


            def g() -> None:
                pass
            '''
        ),
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
    _ = _write(tmp_path / "scripts/a.py", '"""A."""\n')
    _ = _write(tmp_path / "tests/b.py", '"""B."""\n')
    _ = _write(tmp_path / "other/c.py", '"""C."""\n')
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
    _ = _write(
        tmp_path / "Containerfile",
        "FROM scratch\nCOPY ./src /src\nCOPY ./gone /gone\n",
    )
    (tmp_path / "src").mkdir()
    _ = _write(tmp_path / ".dockerignore", "# c\nsrc/\n")
    found = "\n".join(check_build_context.check_containerfiles(tmp_path))
    assert "./gone missing" in found
    assert "./src excluded by .dockerignore src" in found


def test_packages_flag_incoherent_manifest(tmp_path: Path) -> None:
    """Unknown deps, missing sources, empty builds and unhashed libs fail.

    Args:
        tmp_path: Scratch package builder directory.
    """
    _ = _write(
        tmp_path / "packages.yml",
        textwrap.dedent(
            """\
            packages:
              - name: a
                deps: [ghost]
                sources: [nope]
                build: |
                  . lib/x.sh
              - name: b
            """
        ),
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
    _ = _write(tmp_path / "code.txt", "one\n\nthree\n")
    _ = _write(
        tmp_path / "docs/page.md",
        textwrap.dedent(
            """\
            ok [src: file: code.txt:1]
            blank [src: file: code.txt:2]
            far [src: file: code.txt:9]
            gone [src: file: nothing.txt:1]
            [dead](missing.md) [live](page.md) [web](https://x.y) [wiki](Home)
            """
        ),
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
    assert "Home" not in found  # extensionless wiki link: not checked


def test_docs_skip_kronn_backups(tmp_path: Path) -> None:
    """Pages under the Kronn backup directory are not verified.

    Args:
        tmp_path: Scratch repository.
    """
    _ = _write(tmp_path / "docs/a.md", "x\n")
    _ = _write(tmp_path / "docs/.kronn-citation-originals/b.md", "x\n")
    names = [p.name for p in check_docs.pages(tmp_path / "docs")]
    assert names == ["a.md"]


def test_build_context_without_a_dockerignore_ignores_nothing(
    tmp_path: Path
) -> None:
    """A root with no .dockerignore contributes no pattern.

    Args:
        tmp_path: Scratch repository.
    """
    assert check_build_context.ignore_patterns(tmp_path) == []


def test_packages_without_a_manifest_report_nothing(tmp_path: Path) -> None:
    """A package builder directory with no packages.yml is not a defect.

    Args:
        tmp_path: Scratch package builder directory.
    """
    assert check_build_context.check_packages(tmp_path) == []


@pytest.mark.parametrize("names", [["a", "a"], ["a", ""]])
def test_packages_require_unique_non_empty_names(
    tmp_path: Path, names: list[str]
) -> None:
    """Duplicate and empty package names are refused.

    Args:
        tmp_path: Scratch package builder directory.
        names: Package names written to the manifest.
    """
    entries = "".join(
        f'  - name: "{name}"\n    upstream: https://example.invalid/{i}\n'
        for i, name in enumerate(names)
    )
    _ = _write(tmp_path / "packages.yml", "packages:\n" + entries)
    found = "\n".join(check_build_context.check_packages(tmp_path))
    assert "package names must be unique and non-empty" in found


def test_build_context_cli_passes_a_consistent_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI exits 0 when every copy source exists and packages are sound.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
    """
    (tmp_path / "src").mkdir()
    _ = _write(tmp_path / "Containerfile", "FROM scratch\nCOPY src /src\n")
    monkeypatch.setattr(
        sys, "argv", [
            "check_build_context.py", "--root",
            str(tmp_path), "--packages", "builder"
        ]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(SCRIPTS / "check_build_context.py"), run_name="__main__"
        )
    assert excinfo.value.code == 0


def test_build_context_cli_reports_defects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI exits 1 and prints each defect on stderr.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
        capsys: Output capture.
    """
    _ = _write(tmp_path / "Containerfile", "FROM scratch\nCOPY gone /gone\n")
    _ = _write(tmp_path / "builder/packages.yml", "packages:\n  - name: a\n")
    monkeypatch.setattr(
        sys, "argv", [
            "check_build_context.py", "--root",
            str(tmp_path), "--packages", "builder"
        ]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(SCRIPTS / "check_build_context.py"), run_name="__main__"
        )
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "gone missing" in err
    assert "a: neither upstream nor build" in err


def test_docs_line_less_citations_check_only_the_file(tmp_path: Path) -> None:
    """A citation naming no line holds when its file exists; ranges resolve.

    Args:
        tmp_path: Scratch repository.
    """
    _ = _write(tmp_path / "code.txt", "one\ntwo\n")
    page = _write(
        tmp_path / "docs/page.md",
        "a [src: file: code.txt] b [src: file: code.txt:1-2]\n"
    )
    assert check_docs.check_page(tmp_path, page) == []


def test_docs_citation_before_the_first_line_is_past_the_end(
    tmp_path: Path
) -> None:
    """Line 0 is rejected instead of reading from the end of the file.

    Args:
        tmp_path: Scratch repository.
    """
    _ = _write(tmp_path / "code.txt", "one\n")
    page = _write(
        tmp_path / "docs/page.md",
        "[src: file: code.txt:0] [src: file: gone.txt]\n"
    )
    found = "\n".join(check_docs.check_page(tmp_path, page))
    assert "code.txt:0 is past the end (1 lines)" in found
    assert "gone.txt does not exist" in found


def test_docs_cli_passes_without_a_docs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree with no docs directory has nothing to verify.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
    """
    monkeypatch.setattr(sys, "argv", ["check_docs.py", "--root", str(tmp_path)])
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(str(SCRIPTS / "check_docs.py"), run_name="__main__")
    assert excinfo.value.code == 0


def test_docs_cli_reports_broken_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI exits 1 and prints the defect on stderr.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
        capsys: Output capture.
    """
    _ = _write(tmp_path / "docs/page.md", "[dead](missing.md)\n")
    monkeypatch.setattr(sys, "argv", ["check_docs.py", "--root", str(tmp_path)])
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(str(SCRIPTS / "check_docs.py"), run_name="__main__")
    assert excinfo.value.code == 1
    assert "broken link missing.md" in capsys.readouterr().err


def test_docstrings_accept_generators_raisers_and_classes(
    tmp_path: Path
) -> None:
    """Yields, Raises, a bare return and a documented class all pass.

    Args:
        tmp_path: Scratch directory.
    """
    source = _write(
        tmp_path / "ok.py",
        textwrap.dedent(
            '''\
            """Mod."""


            class Documented:
                """A documented class."""


            def gen():
                """Yield one.

                Yields:
                    One.
                """
                yield 1


            def boom():
                """Fail.

                Raises:
                    ValueError: Always.
                """
                raise ValueError


            def stop():
                """Return nothing."""
                return
            '''
        ),
    )
    assert check_docstrings.problems(source) == []


def test_docstrings_flag_a_missing_module_docstring(tmp_path: Path) -> None:
    """A file without a module docstring is reported.

    Args:
        tmp_path: Scratch directory.
    """
    source = _write(tmp_path / "bare.py", "x = 1\n")
    found = "\n".join(check_docstrings.problems(source))
    assert "module docstring missing" in found


def test_docstrings_collect_reads_explicit_files(tmp_path: Path) -> None:
    """Explicit files are collected and unknown paths are skipped.

    Args:
        tmp_path: Scratch repository.
    """
    _ = _write(tmp_path / "scripts/a.py", '\"\"\"A.\"\"\"\n')
    found = check_docstrings.collect(tmp_path, ["scripts/a.py", "ghost"])
    assert [p.name for p in found] == ["a.py"]


def test_docstrings_cli_passes_a_documented_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI exits 0 when every scanned file is documented.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
    """
    _ = _write(tmp_path / "scripts/a.py", '\"\"\"A.\"\"\"\n')
    monkeypatch.setattr(
        sys, "argv", ["check_docstrings.py", "--root",
                      str(tmp_path)]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(SCRIPTS / "check_docstrings.py"), run_name="__main__"
        )
    assert excinfo.value.code == 0


def test_docstrings_cli_reports_undocumented_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI exits 1 and names each undocumented file.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
        capsys: Output capture.
    """
    _ = _write(tmp_path / "tests/b.py", "x = 1\n")
    monkeypatch.setattr(
        sys, "argv", ["check_docstrings.py", "--root",
                      str(tmp_path)]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(SCRIPTS / "check_docstrings.py"), run_name="__main__"
        )
    assert excinfo.value.code == 1
    assert "module docstring missing" in capsys.readouterr().err


def test_manifest_cli_passes_a_sound_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI exits 0 on the sound manifest.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
    """
    _ = _write(tmp_path / "workflows/tests.yml", MANIFEST_YAML)
    monkeypatch.setattr(
        sys, "argv", ["check_tests_manifest.py", "--root",
                      str(tmp_path)]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(SCRIPTS / "check_tests_manifest.py"), run_name="__main__"
        )
    assert excinfo.value.code == 0


def test_manifest_cli_reports_defects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI exits 1 and prefixes each defect with the manifest path.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
        capsys: Output capture.
    """
    _ = _write(tmp_path / "workflows/tests.yml", "tests: []\n")
    monkeypatch.setattr(
        sys, "argv", ["check_tests_manifest.py", "--root",
                      str(tmp_path)]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(SCRIPTS / "check_tests_manifest.py"), run_name="__main__"
        )
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "workflows/tests.yml: no tests declared" in err
    assert "boot_timeout must be a positive integer" in err


def test_manifest_cli_fails_when_the_manifest_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing manifest is an error naming its path.

    Args:
        tmp_path: Scratch repository.
        monkeypatch: Environment patcher.
        capsys: Output capture.
    """
    monkeypatch.setattr(
        sys, "argv", ["check_tests_manifest.py", "--root",
                      str(tmp_path)]
    )
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(
            str(SCRIPTS / "check_tests_manifest.py"), run_name="__main__"
        )
    assert excinfo.value.code == 1
    assert "manifest not found" in capsys.readouterr().err
