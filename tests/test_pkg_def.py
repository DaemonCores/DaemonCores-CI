"""Parity test for ``pkg-def/pkg-def.sh`` against the former inline workflow step.

The step used to live in ``build-one-deb.yml``; both are run on the same
fixture manifest and must append identical lines to ``GITHUB_OUTPUT``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'pkg-def' / 'pkg-def.sh'
OLD_REV = 'fc585a9'

MANIFEST = """
packages:
  - name: demo
    upstream: demo-upstream
    deb-glob: demo_*
    deps: [libfoo, libbar]
    sources: [demo, shared]
    env:
      VERSION: "1.2"
    secret-env:
      GH_TOKEN: GITHUB_TOKEN
    patches:
      - file: a.patch
    build: |
      kernel_signing_setup
      echo build
  - name: plain
    sources: [plain]
    build: |
      echo plain
"""

pytestmark = pytest.mark.skipif(shutil.which('yq') is None,
                                reason='yq is required')


def old_step() -> str:
    """Return the former inline ``Read package definition`` script.

    Returns:
        The step's ``run`` text at ``OLD_REV``.
    """
    text = subprocess.run(
        ['git', 'show', f'{OLD_REV}:.github/workflows/build-one-deb.yml'],
        cwd=ROOT, check=True, capture_output=True, text=True).stdout
    workflow = yaml.safe_load(text)
    for job in workflow['jobs'].values():
        for step in job['steps']:
            if step.get('id') == 'def':
                return step['run']
    raise AssertionError('step def not found')


def run(script: Path, tmp: Path, pkg: str) -> str:
    """Run a script on the fixture and return the lines it appended.

    Args:
        script: Script to execute.
        tmp: Scratch directory.
        pkg: Package name.

    Returns:
        The content of the output file.
    """
    manifest = tmp / 'manifest.yml'
    manifest.write_text(MANIFEST)
    out = tmp / f'{script.stem}-{pkg}.out'
    out.write_text('')
    subprocess.run(
        ['bash', str(script)],
        env={'PATH': f'{Path(shutil.which("yq")).parent}:/usr/bin:/bin',
             'MAN': str(manifest), 'PKG': pkg, 'ARCH': 'amd64',
             'GITHUB_OUTPUT': str(out), 'SECRETVAL_GITHUB_TOKEN': 'tok-123'},
        check=True, capture_output=True, text=True)
    return out.read_text()


@pytest.mark.parametrize('pkg', ['demo', 'plain'])
def test_matches_inline_step(tmp_path: Path, pkg: str) -> None:
    """The extracted script writes the same outputs as the old step."""
    old = tmp_path / 'old.sh'
    old.write_text('set -euo pipefail\n' + old_step())
    assert run(SCRIPT, tmp_path, pkg) == run(old, tmp_path, pkg)


def test_signs_flag_and_secret_resolved(tmp_path: Path) -> None:
    """A kernel-signing build is flagged and its secret env is resolved."""
    out = run(SCRIPT, tmp_path, 'demo')
    assert 'signs=yes' in out
    assert 'GH_TOKEN=tok-123' in out
    assert 'deps=libfoo libbar' in out
