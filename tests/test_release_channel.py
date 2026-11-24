"""Tests for scripts/release-channel.sh."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__
              ).resolve().parent.parent / 'scripts' / 'release-channel.sh'


def _run(ref: str) -> subprocess.CompletedProcess[str]:
    """Run the script for one branch name.

    Args:
        ref: Branch name.

    Returns:
        The completed process.
    """
    return subprocess.run(['bash', str(SCRIPT), ref],
                          capture_output=True,
                          text=True,
                          check=False)


def test_develop_is_edge_and_main_is_stable() -> None:
    """The two git-flow branches map to their channel."""
    assert _run('develop').stdout == 'edge\n'
    assert _run('main').stdout == 'stable\n'


def test_other_refs_have_no_channel() -> None:
    """A feature branch must not publish into any channel."""
    for ref in ('feature/x', 'security', ''):
        done = _run(ref)
        assert done.returncode != 0, ref
        assert 'has no release channel' in done.stderr
