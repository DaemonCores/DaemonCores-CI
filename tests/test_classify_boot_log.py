"""Unit tests for ``scripts/classify_boot_log.py`` (ISO smoke verdict).

The fixtures in ``tests/fixtures/boot-logs`` are trimmed excerpts (first 25
lines + the lines around the marker) of real serial logs captured while
booting the published live ISOs in QEMU:

* ``amd64-live-login.log``: the amd64 ISO reaching the live-root login,
* ``amd64-live-booting.log``: the same boot, cut just before the login,
* ``arm64-initrd-emergency.log``: the arm64 ISO stopping in dracut emergency,
* ``arm64-kernel-stall.log``: the whole serial console of the arm64 smoke that
  timed out in run 45087765493, before any ``Freeing unused kernel memory``.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import classify_boot_log

SCRIPT = Path(classify_boot_log.__file__)
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'boot-logs'


def fixture(name: str) -> str:
    """Read a boot log fixture.

    Args:
        name: File name under ``tests/fixtures/boot-logs``.

    Returns:
        The log text.
    """
    return (FIXTURES / name).read_text(errors='replace')


def test_login_prompt_of_the_live_root_is_up() -> None:
    """A real log that reached the live-root login is a success."""
    assert classify_boot_log.classify(fixture('amd64-live-login.log')) == 'up'


def test_a_log_cut_before_the_login_is_not_up() -> None:
    """Reaching the initrd targets, services and mounts proves nothing."""
    log = fixture('amd64-live-booting.log')
    assert 'Reached target' in log or 'OK' in log
    assert classify_boot_log.classify(log) == 'booting'


def test_dracut_emergency_is_detected() -> None:
    """The arm64 initrd emergency shell fails the smoke."""
    assert classify_boot_log.classify(
        fixture('arm64-initrd-emergency.log')
    ) == 'emergency'


def test_boot_loop_is_detected_even_though_the_kernel_runs_every_time() -> None:
    """Two kernel banners and no login: the ISO restarts forever.

    Synthetic: the same real boot, repeated, as a guest that reboots after
    the initramfs would produce. The old ISO showed exactly this and a
    success marker looking at initrd targets let it through.
    """
    one_boot = fixture('amd64-live-booting.log')
    assert classify_boot_log.classify(one_boot + one_boot) == 'bootloop'


def test_boot_loop_wins_over_an_earlier_login() -> None:
    """A guest that logs in then reboots has not booted stably."""
    one_boot = fixture('amd64-live-login.log')
    assert classify_boot_log.classify(one_boot + one_boot) == 'bootloop'


def test_emergency_wins_over_a_login_prompt() -> None:
    """A failure marker is never masked by a prompt seen in the same log."""
    log = fixture('amd64-live-login.log') + 'Entering emergency mode.\n'
    assert classify_boot_log.classify(log) == 'emergency'


@pytest.mark.parametrize(
    'text',
    [
        'Reached target initrd-switch-root.target\n',
        'Reached target Basic System.\n',
        'Linux version 7.2.15\n',
        '',
    ],
)
def test_initrd_progress_is_never_a_success(text: str) -> None:
    """Initrd targets and the kernel banner alone leave the state open.

    Args:
        text: A log that shows boot progress but no live-root login.
    """
    assert classify_boot_log.classify(text) == 'booting'


def test_command_line_prints_the_state() -> None:
    """The CLI the workflow calls prints the state of the file."""
    out = subprocess.run(
        [
            sys.executable, '-I',
            str(SCRIPT),
            str(FIXTURES / 'amd64-live-login.log')
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == 'up'


def test_command_line_missing_log_is_booting(tmp_path: Path) -> None:
    """QEMU has not created the log yet: nothing conclusive.

    Args:
        tmp_path: Empty directory, so the log file does not exist.
    """
    out = subprocess.run(
        [sys.executable, '-I',
         str(SCRIPT),
         str(tmp_path / 'absent.log')],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == 'booting'


def test_the_arm64_smoke_stall_is_kernel_side() -> None:
    """Run 45087765493: the kernel never printed the hand-over to /init.

    The whole 900 s serial console of that smoke ends on ``PM: genpd:
    Disabling unused power domains``; the kernel runs that line before
    ``Freeing unused kernel memory``. Without this distinction the timeout
    blamed "the initramfs", which the same kernel disproved locally.
    """
    log = fixture('arm64-kernel-stall.log')
    assert 'Linux version ' in log
    assert 'Freeing unused kernel memory' not in log
    assert 'Run /init as init process' not in log
    assert classify_boot_log.classify(log) == 'booting'
    assert classify_boot_log.stage(log) == 'kernel'


def test_a_boot_that_handed_over_is_userspace() -> None:
    """``Run /init`` means the initramfs started: a stall is in userspace."""
    assert classify_boot_log.stage(
        fixture('arm64-initrd-emergency.log')
    ) == 'userspace'


@pytest.mark.parametrize(
    'name',
    ['amd64-live-login.log', 'arm64-initrd-emergency.log'],
)
def test_a_conclusive_state_is_never_reported_as_a_kernel_stall(
    name: str,
) -> None:
    """A login prompt or an emergency shell proves userspace ran.

    Args:
        name: Fixture reaching either the login or the emergency shell.
    """
    assert classify_boot_log.stage(fixture(name)) == 'userspace'


def test_command_line_prints_the_stage() -> None:
    """``--stage`` answers where a stalled boot stopped."""
    out = subprocess.run(
        [
            sys.executable, '-I',
            str(SCRIPT), '--stage',
            str(FIXTURES / 'arm64-kernel-stall.log')
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == 'kernel'
