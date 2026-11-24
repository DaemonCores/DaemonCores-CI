"""Tests for scripts/security-containerfile.sh."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve(
).parent.parent / 'scripts' / 'security-containerfile.sh'

DIGEST = 'sha256:' + 'a' * 64
UPGRADES = (
    'curl 8.14.1-2 8.14.1-2+deb13u1\n'
    'libssl3t64 3.5.1-1 3.5.2-1~deb13u1\n'
)


def _run(stdin: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script.

    Args:
        stdin: Text given on standard input.
        *args: Arguments given to the script.

    Returns:
        The completed process.
    """
    return subprocess.run(['bash', str(SCRIPT), *args],
                          input=stdin,
                          capture_output=True,
                          text=True,
                          check=False)


def test_rebuild_starts_from_the_stable_digest_with_pinned_upgrades() -> None:
    """The base is the stable digest and each package is pinned."""
    out = _run(UPGRADES, 'ghcr.io/o/r', DIGEST)
    assert out.returncode == 0
    lines = out.stdout.splitlines()
    assert lines[0] == f'FROM ghcr.io/o/r@{DIGEST}'
    assert 'curl=8.14.1-2+deb13u1 libssl3t64=3.5.2-1~deb13u1' in out.stdout
    assert '--only-upgrade' in out.stdout


def test_invalid_digest_is_refused() -> None:
    """A tag or a short digest cannot be used as a pin."""
    out = _run(UPGRADES, 'ghcr.io/o/r', 'stable')
    assert out.returncode != 0
    assert out.stdout == ''


def test_empty_upgrade_list_is_refused() -> None:
    """Without a security upgrade no Containerfile is produced."""
    out = _run('', 'ghcr.io/o/r', DIGEST)
    assert out.returncode != 0
    assert out.stdout == ''
