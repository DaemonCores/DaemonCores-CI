"""Unit tests for ``scripts/patch_guard.py``."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import patch_guard

PATCH = """--- a/kernel/sched/core.c
+++ b/kernel/sched/core.c
@@ -1 +1 @@
-a
+b
--- /dev/null
+++ b/kernel/sched/new.c
@@ -0,0 +1 @@
+x
"""


@pytest.fixture(name="env")
def fixture_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a tree, a patch and a lock path.

    Args:
        tmp_path: Pytest scratch directory.

    Returns:
        The tree, the patch file and the lock path.
    """
    tree = tmp_path / "tree"
    (tree / "kernel" / "sched").mkdir(parents=True)
    (tree / "kernel" / "sched" / "core.c").write_text("a\n")
    patch = tmp_path / "p.patch"
    patch.write_text(PATCH)
    return tree, patch, tmp_path / "lock.json"


def test_touched_files_skips_created_files() -> None:
    """A file the patch creates has no reference and is not listed."""
    assert patch_guard.touched_files(PATCH) == {"kernel/sched/core.c"}


def test_update_then_check_passes(env: tuple[Path, Path, Path]) -> None:
    """A lock written by ``--update`` accepts the same tree.

    Args:
        env: Tree, patch and lock path.
    """
    tree, patch, lock = env
    args = ["--tree", str(tree), "--lock", str(lock), str(patch)]
    assert patch_guard.main([*args, "--update"]) == 0
    expected = hashlib.sha256(b"a\n").hexdigest()
    assert json.loads(lock.read_text()) == {"kernel/sched/core.c": expected}
    assert patch_guard.main(args) == 0


def test_changed_file_is_refused(
    env: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A touched file that changed upstream fails the guard.

    Args:
        env: Tree, patch and lock path.
        capsys: Pytest output capture.
    """
    tree, patch, lock = env
    args = ["--tree", str(tree), "--lock", str(lock), str(patch)]
    patch_guard.main([*args, "--update"])
    (tree / "kernel" / "sched" / "core.c").write_text("changed\n")
    assert patch_guard.main(args) == 1
    assert "kernel/sched/core.c: sha256 changed" in capsys.readouterr().out


def test_file_without_reference_is_refused(
    env: tuple[Path, Path, Path],
) -> None:
    """A touched file absent from the lock fails the guard.

    Args:
        env: Tree, patch and lock path.
    """
    tree, patch, lock = env
    lock.write_text("{}\n")
    args = ["--tree", str(tree), "--lock", str(lock), str(patch)]
    assert patch_guard.main(args) == 1


def test_update_merges_with_the_existing_lock(
    env: tuple[Path, Path, Path],
) -> None:
    """``--update`` keeps the references of files other runs recorded.

    Args:
        env: Tree, patch and lock path.
    """
    tree, patch, lock = env
    lock.write_text('{"other.c": "abc"}\n')
    args = ["--tree", str(tree), "--lock", str(lock), str(patch), "--update"]
    assert patch_guard.main(args) == 0
    assert set(json.loads(lock.read_text())
               ) == {"other.c", "kernel/sched/core.c"}
