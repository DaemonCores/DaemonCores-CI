"""Contract tests for ``deb_src_overlay`` (overlay on an upstream source tree)."""

import hashlib
import json
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def run(overlay: Path, tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{SCRIPTS}/deb_helpers.sh"; deb_src_overlay "{overlay}" "{tree}"'],
        env={"CI_SCRIPTS": str(SCRIPTS), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )


def make_tree(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src/a.txt").write_text("one\ntwo\n")
    return root


def make_overlay(root: Path, *, lock_sha: str | None = None) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src/a.txt.patch").write_text(
        "--- a/src/a.txt\n+++ b/src/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+three\n"
    )
    (root / "src/new.txt").write_text("fresh\n")
    sha = lock_sha or hashlib.sha256(b"one\ntwo\n").hexdigest()
    (root / "patches.lock.json").write_text(json.dumps({"src/a.txt": sha}))
    return root


def test_new_file_and_patch_are_applied(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "tree")
    result = run(make_overlay(tmp_path / "ovl"), tree)
    assert result.returncode == 0, result.stderr
    assert (tree / "src/a.txt").read_text() == "one\nthree\n"
    assert (tree / "src/new.txt").read_text() == "fresh\n"
    assert not list(tree.rglob("*.patch"))


def test_lock_drift_stops_with_code_3_and_leaves_the_tree(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "tree")
    result = run(make_overlay(tmp_path / "ovl", lock_sha="0" * 64), tree)
    assert result.returncode == 3
    assert (tree / "src/a.txt").read_text() == "one\ntwo\n"
    assert not (tree / "src/new.txt").exists()


def test_patch_drift_is_all_or_nothing(tmp_path: Path) -> None:
    tree = make_tree(tmp_path / "tree")
    (tree / "src/a.txt").write_text("one\nTWO\n")
    result = run(make_overlay(tmp_path / "ovl", lock_sha=hashlib.sha256(b"one\nTWO\n").hexdigest()), tree)
    assert result.returncode == 3
    assert not (tree / "src/new.txt").exists()


def run_apply(overlay: Path, tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{SCRIPTS}/deb_helpers.sh"; deb_overlay_apply "{overlay}" "{tree}"'],
        env={"CI_SCRIPTS": str(SCRIPTS), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )


def test_delete_marker_removes_the_file_whatever_its_content(tmp_path: Path) -> None:
    for content in ("one\n", "a completely different revision\n"):
        tree = tmp_path / f"tree-{len(content)}"
        (tree / "etc").mkdir(parents=True)
        (tree / "etc/hook").write_text(content)
        (tree / "etc/keep").write_text("keep\n")
        ovl = tmp_path / f"ovl-{len(content)}"
        (ovl / "etc").mkdir(parents=True)
        (ovl / "etc/hook.delete").write_text("")
        result = run_apply(ovl, tree)
        assert result.returncode == 0, result.stderr
        assert not (tree / "etc/hook").exists()
        assert (tree / "etc/keep").exists()
        assert not list(tree.rglob("*.delete"))


def test_delete_marker_on_missing_file_is_drift(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "etc").mkdir(parents=True)
    ovl = tmp_path / "ovl"
    (ovl / "etc").mkdir(parents=True)
    (ovl / "etc/hook.delete").write_text("")
    (ovl / "etc/new").write_text("x\n")
    result = run_apply(ovl, tree)
    assert result.returncode == 3
    assert not (tree / "etc/new").exists()
