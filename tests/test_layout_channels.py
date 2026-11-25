"""Tests for bootc-debs-end/layout-channels.sh."""

import hashlib
import os
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent / "bootc-debs-end" /
    "layout-channels.sh"
)


def _publish(site: Path, channel: str, body: bytes) -> bytes:
    """Write a minimal signed-looking channel repository.

    Args:
        site: Published site directory.
        channel: Channel directory name.
        body: Content of the single pool file.

    Returns:
        The pool file content.
    """
    root = site / channel
    dist = root / "dists" / "trixie"
    (dist / "main" / "binary-amd64").mkdir(parents=True)
    (root / "pool" / "main").mkdir(parents=True)
    _ = (root / "pool" / "main" / "a.deb").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    _ = (dist / "main" / "binary-amd64" / "Packages").write_text(
        f"Package: a\nFilename: pool/main/a.deb\nSHA256: {digest}\n",
        encoding="utf-8",
    )
    _ = (dist / "Release").write_text(
        "Suite: trixie\nSHA256:\n 00 1 main/binary-amd64/Packages\n",
        encoding="utf-8",
    )
    _ = (dist / "InRelease").write_text("signed", encoding="utf-8")
    _ = (root / "gpg.key").write_text("key", encoding="utf-8")
    return body


def _run(built: Path,
         channel: str,
         published: Path,
         drop: str = "") -> subprocess.CompletedProcess[str]:
    """Run the script.

    Args:
        built: Directory with the freshly built repository.
        channel: Channel of this run.
        published: Live site directory.
        drop: Channels to unpublish.

    Returns:
        The completed process.
    """
    env = dict(
        os.environ,
        SITE_DIR=str(built),
        CHANNEL=channel,
        PAGES_URL=f"file://{published}",
        DROP_CHANNELS=drop,
    )
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _built(tmp_path: Path) -> Path:
    """Create the output directory of a build.

    Args:
        tmp_path: Scratch directory.

    Returns:
        A directory with a repository, the deb cache and the SBOM.
    """
    built = tmp_path / "built"
    (built / "dists").mkdir(parents=True)
    (built / "debs-cache").mkdir()
    _ = (built / "sbom.json").write_text("{}", encoding="utf-8")
    _ = (built / "gpg.key").write_text("new", encoding="utf-8")
    return built


def test_own_channel_moves_and_siblings_are_carried(tmp_path: Path) -> None:
    """The new repo goes under its channel, the live siblings come back.

    Args:
        tmp_path: Scratch directory.
    """
    live = tmp_path / "live"
    _ = _publish(live, "edge", b"edge-deb")
    _ = _publish(live, "security", b"sec-deb")
    built = _built(tmp_path)
    done = _run(built, "stable", live)
    assert done.returncode == 0, done.stderr
    assert (built / "stable" / "gpg.key").read_text() == "new"
    assert (built / "debs-cache").is_dir()
    assert (built / "sbom.json").is_file()
    assert (built / "edge" / "pool" / "main" /
            "a.deb").read_bytes() == b"edge-deb"
    assert (built / "security" / "pool" / "main" /
            "a.deb").read_bytes() == b"sec-deb"
    assert (built / "edge" / "dists" / "trixie" / "InRelease").is_file()


def test_unpublished_sibling_is_not_an_error(tmp_path: Path) -> None:
    """A channel never published is skipped.

    Args:
        tmp_path: Scratch directory.
    """
    built = _built(tmp_path)
    done = _run(built, "edge", tmp_path / "nothing")
    assert done.returncode == 0, done.stderr
    assert (built / "edge" / "gpg.key").is_file()
    assert not (built / "stable").exists()


def test_corrupt_sibling_pool_file_stops_the_publication(
    tmp_path: Path,
) -> None:
    """A sibling pool file that differs from its checksum is an error.

    Args:
        tmp_path: Scratch directory.
    """
    live = tmp_path / "live"
    _ = _publish(live, "edge", b"edge-deb")
    _ = (live / "edge" / "pool" / "main" / "a.deb").write_bytes(b"tampered")
    done = _run(_built(tmp_path), "stable", live)
    assert done.returncode != 0
    assert "differs from its published checksum" in done.stderr


def test_unknown_channel_is_refused(tmp_path: Path) -> None:
    """Only a known channel may publish.

    Args:
        tmp_path: Scratch directory.
    """
    done = _run(_built(tmp_path), "nightly", tmp_path / "live")
    assert done.returncode != 0
    assert "channel 'nightly' is not one of" in done.stderr


def test_dropped_channel_is_not_carried(tmp_path: Path) -> None:
    """A channel listed to drop disappears, the others stay.

    Args:
        tmp_path: Scratch directory.
    """
    live = tmp_path / "live"
    _ = _publish(live, "stable", b"dev-deb")
    _ = _publish(live, "security", b"sec-deb")
    built = _built(tmp_path)
    done = _run(built, "edge", live, drop="stable")
    assert done.returncode == 0, done.stderr
    assert not (built / "stable").exists()
    assert (built / "security" / "gpg.key").is_file()


def test_action_wires_drop_channels_without_the_vars_context() -> None:
    """The composite action takes the list as an input.

    The `vars` context does not exist in composite actions: the workflow passes
    it as an input (a run failed on it at template validation).
    """
    action = (SCRIPT.parent / "action.yml").read_text(encoding="utf-8")
    assert "DROP_CHANNELS: ${{ inputs.drop_channels }}" in action
    assert "${{ vars." not in action
