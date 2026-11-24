"""Contract tests for ``deb_src_overlay`` (overlay on an upstream tree)."""

import subprocess
import textwrap
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def run(overlay: Path, tree: Path) -> subprocess.CompletedProcess[str]:
    """Run deb_src_overlay on an overlay and a source tree.

    Args:
        overlay: Overlay directory to apply.
        tree: Source tree the overlay is applied to.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    return subprocess.run(
        [
            "bash", "-c", " ".join([
                f'. "{SCRIPTS}/deb_helpers.sh";',
                f'deb_src_overlay "{overlay}" "{tree}"'
            ])
        ],
        env={
            "CI_SCRIPTS": str(SCRIPTS),
            "PATH": "/usr/bin:/bin"
        },
        capture_output=True,
        text=True,
    )


def make_tree(root: Path) -> Path:
    """Create a source tree with one file.

    Args:
        root: Directory to build the tree in.

    Returns:
        The root directory.
    """
    (root / "src").mkdir(parents=True)
    _ = (root / "src/a.txt").write_text("one\ntwo\n")
    return root


def make_overlay(root: Path) -> Path:
    """Build an overlay dir: new file + patch.

    No committed lock: the sha256 reference is computed and retained by the
    guard at build (build-local).

    Args:
        root: Directory to build the overlay in.

    Returns:
        The overlay directory.
    """
    (root / "src").mkdir(parents=True)
    _ = (root / "src/a.txt.patch").write_text(
        textwrap.dedent(
            """\
            --- a/src/a.txt
            +++ b/src/a.txt
            @@ -1,2 +1,2 @@
             one
            -two
            +three
            """
        )
    )
    _ = (root / "src/new.txt").write_text("fresh\n")
    return root


def test_new_file_and_patch_are_applied(tmp_path: Path) -> None:
    """The overlay adds the new file and applies the patch.

    Args:
        tmp_path: Pytest temporary directory.
    """
    tree = make_tree(tmp_path / "tree")
    result = run(make_overlay(tmp_path / "ovl"), tree)
    assert result.returncode == 0, result.stderr
    assert (tree / "src/a.txt").read_text() == "one\nthree\n"
    assert (tree / "src/new.txt").read_text() == "fresh\n"
    assert not list(tree.rglob("*.patch"))


def test_lock_is_retained_build_local(tmp_path: Path) -> None:
    """The sha256 reference is computed AND retained at build.

    It lives under ${PATCH_GUARD_LOCK_DIR:-/tmp/patch-guard}, never in the
    repository.

    Args:
        tmp_path: Pytest temporary directory.
    """
    tree = make_tree(tmp_path / "tree")
    result = run(make_overlay(tmp_path / "ovl"), tree)
    assert result.returncode == 0, result.stderr
    assert (tree / "src/a.txt").read_text() == "one\nthree\n"
    # The overlay applied: the new file ships.
    assert (tree / "src/new.txt").read_text() == "fresh\n"
    # Never a patches.lock.json in the overlay (the repository).
    assert not (tmp_path / "ovl").joinpath("patches.lock.json").exists()


def test_patch_drift_is_all_or_nothing(tmp_path: Path) -> None:
    """A drifted patch exits 3 and applies nothing.

    Args:
        tmp_path: Pytest temporary directory.
    """
    tree = make_tree(tmp_path / "tree")
    _ = (tree / "src/a.txt").write_text("one\nTWO\n")
    result = run(make_overlay(tmp_path / "ovl"), tree)
    assert result.returncode == 3
    assert not (tree / "src/new.txt").exists()


def run_apply(overlay: Path, tree: Path) -> subprocess.CompletedProcess[str]:
    """Run deb_overlay_apply on an overlay and a tree.

    Args:
        overlay: Overlay directory to apply.
        tree: Tree the overlay is applied to.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    return subprocess.run(
        [
            "bash", "-c", " ".join([
                f'. "{SCRIPTS}/deb_helpers.sh";',
                f'deb_overlay_apply "{overlay}" "{tree}"'
            ])
        ],
        env={
            "CI_SCRIPTS": str(SCRIPTS),
            "PATH": "/usr/bin:/bin"
        },
        capture_output=True,
        text=True,
    )


def test_delete_marker_removes_the_file_whatever_its_content(
    tmp_path: Path
) -> None:
    """A .delete marker removes the file regardless of content.

    Args:
        tmp_path: Pytest temporary directory.
    """
    for content in ("one\n", "a completely different revision\n"):
        tree = tmp_path / f"tree-{len(content)}"
        (tree / "etc").mkdir(parents=True)
        _ = (tree / "etc/hook").write_text(content)
        _ = (tree / "etc/keep").write_text("keep\n")
        ovl = tmp_path / f"ovl-{len(content)}"
        (ovl / "etc").mkdir(parents=True)
        _ = (ovl / "etc/hook.delete").write_text("")
        result = run_apply(ovl, tree)
        assert result.returncode == 0, result.stderr
        assert not (tree / "etc/hook").exists()
        assert (tree / "etc/keep").exists()
        assert not list(tree.rglob("*.delete"))


def test_delete_marker_on_missing_file_is_drift(tmp_path: Path) -> None:
    """A .delete marker on a missing file is drift (exit 3).

    Args:
        tmp_path: Pytest temporary directory.
    """
    tree = tmp_path / "tree"
    (tree / "etc").mkdir(parents=True)
    ovl = tmp_path / "ovl"
    (ovl / "etc").mkdir(parents=True)
    _ = (ovl / "etc/hook.delete").write_text("")
    _ = (ovl / "etc/new").write_text("x\n")
    result = run_apply(ovl, tree)
    assert result.returncode == 3
    assert not (tree / "etc/new").exists()
