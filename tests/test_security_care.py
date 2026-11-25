"""Tests for scripts/security-care.sh."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "security-care.sh"

HOUR = 3600
DELAY = 24


def _run(upgrades: str,
         ledger: Path,
         now: int,
         urgent: str = "") -> subprocess.CompletedProcess[str]:
    """Run the care delay on a list of upgrades.

    Args:
        upgrades: Lines of security-detect.sh output.
        ledger: First-seen ledger file.
        now: Current epoch seconds.
        urgent: Newline separated packages that skip the delay.

    Returns:
        The completed process; stdout holds the adopted upgrades.
    """
    return subprocess.run(
        ["bash", str(SCRIPT),
         str(ledger), str(DELAY),
         str(now)],
        input=upgrades,
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "SECURITY_URGENT": urgent
        },
    )


def test_new_version_waits_for_the_care_delay(tmp_path: Path) -> None:
    """A version seen for the first time is held back and recorded.

    Args:
        tmp_path: Pytest temporary directory holding the ledger.
    """
    ledger = tmp_path / "ledger"
    out = _run("curl 8.14.1-2 8.14.1-2+deb13u1\n", ledger, 1000).stdout
    assert out == ""
    assert ledger.read_text() == "curl 8.14.1-2+deb13u1 1000\n"


def test_version_is_adopted_once_old_enough(tmp_path: Path) -> None:
    """After the delay the version goes through, unchanged.

    Args:
        tmp_path: Pytest temporary directory holding the ledger.
    """
    ledger = tmp_path / "ledger"
    _ = ledger.write_text("curl 8.14.1-2+deb13u1 1000\n")
    line = "curl 8.14.1-2 8.14.1-2+deb13u1\n"
    assert _run(line, ledger, 1000 + DELAY * HOUR - 1).stdout == ""
    assert _run(line, ledger, 1000 + DELAY * HOUR).stdout == line


def test_urgent_package_skips_the_delay(tmp_path: Path) -> None:
    """A package named by the critical feed is adopted at once.

    Args:
        tmp_path: Pytest temporary directory holding the ledger.
    """
    ledger = tmp_path / "ledger"
    line = "curl 8.14.1-2 8.14.1-2+deb13u1\n"
    assert _run(line, ledger, 1000, urgent="curl\n").stdout == line
