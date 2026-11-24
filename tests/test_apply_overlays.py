"""Unit tests for ``apply_overlays.sh`` (deb-tree overlay with patch loop)."""

import subprocess
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "apply_overlays.sh")


def run(overlay: Path, tree: Path) -> subprocess.CompletedProcess[str]:
    """Run apply_overlays.sh on an overlay and a deb tree.

    Args:
        overlay: Overlay directory to apply.
        tree: Deb tree the overlay is applied to.

    Returns:
        The finished process, with stdout and stderr as text.
    """
    return subprocess.run(
        ["bash", str(SCRIPT), str(overlay),
         str(tree)],
        capture_output=True,
        text=True,
    )


def touch(path: Path, text: str) -> None:
    """Write text to path, creating parent directories.

    Args:
        path: File to write.
        text: Content to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(text)


def make_tree(root: Path) -> Path:
    """Create a minimal deb tree under root.

    Args:
        root: Directory to build the tree in.

    Returns:
        The root directory.
    """
    touch(root / "usr/share/perl5/PVE/Up.pm", "one\n")
    touch(root / "usr/share/perl5/PVE/Two.pm", "line1\nline2\n")
    touch(root / "DEBIAN/control", "Package: t\n")
    return root


def make_overlay(root: Path, *, drifted: bool = False) -> Path:
    """Create an overlay with a new file and one patch.

    Args:
        root: Directory to create the overlay under.
        drifted: Write a patch that no longer applies to the tree.

    Returns:
        The overlay directory.
    """
    # Gabriel spec 2026-10-30: the .patch file sits at the MIRROR PATH of
    # the file it patches, not at the overlay root.
    overlay = root / "overlay"
    touch(overlay / "usr/share/newfile.txt", "new\n")
    if drifted:
        touch(
            overlay / "usr/share/perl5/PVE/Up.pm.patch", (
                "diff --git a/usr/share/perl5/PVE/Up.pm "
                "b/usr/share/perl5/PVE/Up.pm\n"
                "--- a/usr/share/perl5/PVE/Up.pm\n"
                "+++ b/usr/share/perl5/PVE/Up.pm\n"
                "@@ -1 +1 @@\n"
                "-absent-line\n"
                "+replacement\n"
            )
        )
    else:
        touch(
            overlay / "usr/share/perl5/PVE/Two.pm.patch", (
                "diff --git a/usr/share/perl5/PVE/Two.pm "
                "b/usr/share/perl5/PVE/Two.pm\n"
                "--- a/usr/share/perl5/PVE/Two.pm\n"
                "+++ b/usr/share/perl5/PVE/Two.pm\n"
                "@@ -2 +2,2 @@\n"
                " line2\n"
                "+line3\n"
            )
        )
    return overlay


def touch_meta(overlay: Path) -> Path:
    """Add the patches.lock.json meta file to the overlay.

    Args:
        overlay: Overlay directory to add the meta file to.

    Returns:
        The overlay directory.
    """
    touch(overlay / "patches.lock.json", "{}\n")
    return overlay


def test_overlay_copies_tree_and_applies_patches(tmp_path: Path) -> None:
    """Overlay files are copied and patches applied cleanly.

    Args:
        tmp_path: Pytest temporary directory.
    """
    tree = make_tree(tmp_path / "tree")
    overlay = touch_meta(make_overlay(tmp_path, drifted=False))
    result = run(overlay, tree)
    assert result.returncode == 0, result.stderr
    assert (tree / "usr/share/newfile.txt").read_text() == "new\n"
    assert (tree / "DEBIAN/control").read_text() == "Package: t\n"
    two = tree / "usr/share/perl5/PVE/Two.pm"
    assert "line3\n" in two.read_text()
    # the .patch files never land in the deb, at ANY depth, and neither
    # does the meta - the overlay repo keeps its copies untouched
    assert not list(tree.rglob("*.patch"))
    assert not list(tree.rglob("patches.lock.json"))
    assert (overlay / "usr/share/perl5/PVE/Two.pm.patch").exists()
    assert "applied clean" in result.stdout


def test_overlay_refuses_drifted_patch_and_keeps_tree_pristine(
    tmp_path: Path,
) -> None:
    """A drifted patch exits 3 and leaves the tree untouched.

    Args:
        tmp_path: Pytest temporary directory.
    """
    tree = make_tree(tmp_path / "tree")
    overlay = make_overlay(tmp_path, drifted=True)
    result = run(overlay, tree)
    # exit 3 = drift, caller skips the build; no partial application
    assert result.returncode == 3
    assert "does not apply" in result.stdout + result.stderr
    assert (tree / "usr/share/perl5/PVE/Up.pm").read_text() == "one\n"
    assert not (tree / "usr/share/newfile.txt").exists()


def test_overlay_without_patches_copies_only(tmp_path: Path) -> None:
    """An overlay with no patches only copies files.

    Args:
        tmp_path: Pytest temporary directory.
    """
    tree = make_tree(tmp_path / "tree")
    overlay = tmp_path / "empty-overlay"
    overlay.mkdir(parents=True)
    result = run(overlay, tree)
    assert result.returncode == 0
    assert "0 patch(es)" in result.stdout


def test_overlay_missing_dirs_fail(tmp_path: Path) -> None:
    """Missing overlay or tree directories fail with a message.

    Args:
        tmp_path: Pytest temporary directory.
    """
    result = run(tmp_path / "nope", tmp_path / "also-nope")
    assert result.returncode != 0
    assert "missing" in result.stderr


def test_overlay_translates_the_triplet_for_arm64(tmp_path: Path) -> None:
    """The multiarch triplet is rewritten for arm64 builds.

    Args:
        tmp_path: Pytest temporary directory.
    """
    # Debian multiarch: the overlay mirror names x86_64-linux-gnu; the arm64
    # deb tree names aarch64-linux-gnu. With BUILD_ARCH=arm64 the helper
    # rewrites the triplet in the patch stream before applying.
    tree = tmp_path / "tree"
    touch(
        tree / "usr/lib/aarch64-linux-gnu/perl5/5.40/Template/Provider.pm",
        "line1\nline2\n",
    )
    overlay = tmp_path / "overlay"
    touch(
        overlay /
        "usr/lib/x86_64-linux-gnu/perl5/5.40/Template/Provider.pm.patch", (
            "diff --git "
            "a/usr/lib/x86_64-linux-gnu/perl5/5.40/Template/Provider.pm"
            " b/usr/lib/x86_64-linux-gnu/perl5/5.40/Template/Provider.pm\n"
            "--- a/usr/lib/x86_64-linux-gnu/perl5/5.40/Template/Provider.pm\n"
            "+++ b/usr/lib/x86_64-linux-gnu/perl5/5.40/Template/Provider.pm\n"
            "@@ -2 +2,2 @@\n"
            " line2\n"
            "+line3\n"
        )
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(overlay),
         str(tree)],
        capture_output=True,
        text=True,
        env={
            "BUILD_ARCH": "arm64",
            "PATH": "/usr/bin:/bin:/usr/sbin"
        },
    )
    assert result.returncode == 0, result.stderr
    assert "line3\n" in (
        tree / "usr/lib/aarch64-linux-gnu/perl5/5.40/Template/Provider.pm"
    ).read_text()
