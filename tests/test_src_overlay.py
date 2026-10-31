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
