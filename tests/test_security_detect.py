"""Tests for scripts/security-detect.sh."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__
              ).resolve().parent.parent / 'scripts' / 'security-detect.sh'

ARCHIVE = 'Debian-Security:13/stable-security [amd64]'

SIMULATION = f"""Reading package lists...
Inst libssl3t64 [3.5.1-1] (3.5.2-1~deb13u1 {ARCHIVE})
Inst tzdata [2025b-5] (2025c-1 Debian:13.1/stable [all])
Inst curl [8.14.1-2] (8.14.1-2+deb13u1 {ARCHIVE})
Conf libssl3t64 (3.5.2-1~deb13u1 {ARCHIVE})
"""


def _run(text: str) -> str:
    """Run the script on a simulated upgrade.

    Args:
        text: Output of apt-get -s upgrade.

    Returns:
        The standard output of the script.
    """
    return subprocess.run(['bash', str(SCRIPT)],
                          input=text,
                          capture_output=True,
                          text=True,
                          check=True).stdout


def test_only_security_upgrades_are_kept() -> None:
    """A regular archive upgrade is not a reason to rebuild."""
    assert _run(SIMULATION) == (
        'curl 8.14.1-2 8.14.1-2+deb13u1\n'
        'libssl3t64 3.5.1-1 3.5.2-1~deb13u1\n'
    )


def test_nothing_to_upgrade_is_empty() -> None:
    """No security upgrade gives an empty list: the rebuild is skipped."""
    assert _run('Reading package lists...\n') == ''
