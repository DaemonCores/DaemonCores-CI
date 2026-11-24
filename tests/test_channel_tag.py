"""Tests for scripts/channel-tag.sh."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'channel-tag.sh'


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the script.

    Args:
        *args: Arguments given to the script.

    Returns:
        The completed process.
    """
    return subprocess.run(['bash', str(SCRIPT), *args],
                          capture_output=True,
                          text=True,
                          check=False)


@pytest.mark.parametrize(('channel', 'tag', 'alias'), [
    ('stable', 'latest', 'stable'),
    ('stable', 'latest-16k', 'stable-16k'),
    ('edge', 'latest', 'edge'),
    ('stable', 'gnome', 'stable-gnome'),
])
def test_alias_of_a_tag(channel: str, tag: str, alias: str) -> None:
    """The latest tag takes the bare channel name, others keep a suffix.

    Args:
        channel: Release channel.
        tag: Image tag.
        alias: Expected alias.
    """
    assert _run(channel, tag).stdout == f'{alias}\n'


def test_missing_argument_fails() -> None:
    """Without a tag no alias is invented."""
    assert _run('stable').returncode != 0
