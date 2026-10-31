"""Unit tests for ``build-deb/plan.py`` (the single-job build order)."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'build-deb' / 'plan.py'
KERNEL = 'kernel_signing_setup; make bindeb-pkg'


def _plan(tmp_path: Path, packages: list[dict]) -> list[str]:
    """Run plan.py on a manifest and return the printed order.

    Args:
        tmp_path: Scratch directory.
        packages: The ``packages`` list of the manifest.

    Returns:
        The package names, in the printed order.
    """
    manifest = tmp_path / 'm.json'
    manifest.write_text(json.dumps({'packages': packages}), encoding='utf-8')
    done = subprocess.run([sys.executable, str(SCRIPT), str(manifest)],
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


def test_dependencies_come_first(tmp_path: Path) -> None:
    """A package is built after everything it depends on.

    Args:
        tmp_path: Scratch directory.
    """
    order = _plan(tmp_path, [
        {'name': 'c', 'deps': ['b']},
        {'name': 'b', 'deps': ['a']},
        {'name': 'a'},
    ])
    assert order == ['a', 'b', 'c']


def test_kernels_precede_their_level_and_modules_follow(
        tmp_path: Path) -> None:
    """Kernels lead their level; kernel modules follow the kernels.

    Args:
        tmp_path: Scratch directory.
    """
    order = _plan(tmp_path, [
        {'name': 'aaa-tool'},
        {'name': 'modules', 'deps': ['kernel'], 'build': KERNEL},
        {'name': 'kernel', 'build': KERNEL},
        {'name': 'zzz-tool'},
    ])
    assert order == ['kernel', 'aaa-tool', 'zzz-tool', 'modules']


def test_usage_error(tmp_path: Path) -> None:
    """Without the manifest argument the script exits with status 2.

    Args:
        tmp_path: Scratch directory.
    """
    done = subprocess.run([sys.executable, str(SCRIPT)],
                          capture_output=True,
                          check=False,
                          cwd=tmp_path)
    assert done.returncode == 2
