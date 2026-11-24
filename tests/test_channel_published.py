"""Tests for scripts/channel-published.sh."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__
              ).resolve().parent.parent / 'scripts' / 'channel-published.sh'


def _run(site: Path, channel: str) -> int:
    """Run the script against a directory standing for the Pages site.

    Args:
        site: Directory served as the site root.
        channel: Channel to look for.

    Returns:
        The exit status.
    """
    return subprocess.run(
        ['bash', str(SCRIPT), site.as_uri(), channel],
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def _publish(site: Path, channel: str) -> None:
    """Create the Release file of a channel.

    Args:
        site: Directory standing for the site root.
        channel: Channel to publish.
    """
    release = site / channel / 'dists' / 'trixie' / 'Release'
    release.parent.mkdir(parents=True)
    _ = release.write_text('Suite: trixie\n', encoding='utf-8')


def test_published_channel_is_reported_published(tmp_path: Path) -> None:
    """A channel with a Release file exits 0.

    Args:
        tmp_path: Scratch directory.
    """
    _publish(tmp_path, 'stable')
    assert _run(tmp_path, 'stable') == 0


def test_never_published_channel_is_reported_absent(tmp_path: Path) -> None:
    """A channel without a Release file exits 1, even if a sibling exists.

    Args:
        tmp_path: Scratch directory.
    """
    _publish(tmp_path, 'edge')
    assert _run(tmp_path, 'stable') == 1


def test_unreadable_site_is_unknown_not_absent() -> None:
    """A site that cannot be reached exits 2: unknown is never published."""
    done = subprocess.run(
        ['bash', str(SCRIPT), 'http://127.0.0.1:1', 'stable'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 2
