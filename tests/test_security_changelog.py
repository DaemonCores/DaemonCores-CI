"""Tests for scripts/security-changelog.sh."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__
              ).resolve().parent.parent / 'scripts' / ('security-changelog.sh')


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


def test_one_row_per_package() -> None:
    """Each upgrade becomes a table row under a dated title."""
    result = _run('curl 8.14.1-2 8.14.1-2+deb13u1\n', '20261124')
    assert result.returncode == 0
    assert '# Security update 20261124' in result.stdout
    assert '| curl | 8.14.1-2 | 8.14.1-2+deb13u1 |' in result.stdout


def test_empty_list_fails() -> None:
    """An empty list is an error, never an empty release."""
    assert _run('', '20261124').returncode == 1


def test_bad_date_fails() -> None:
    """A malformed date is refused."""
    assert _run('curl 1 2\n', 'today').returncode == 1
