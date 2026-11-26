"""Parity test of ``pkg-def/pkg-def.sh`` and the former inline workflow step.

The step used to live in ``build-one-deb.yml``; both are run on the same fixture
manifest and must append identical lines to ``GITHUB_OUTPUT``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from typed_docs import as_mapping, as_str, iter_steps, load_yaml

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

pytestmark = pytest.mark.skipif(
    shutil.which('yq') is None, reason='yq is required'
)


def _yq_dir() -> str:
    """Return the directory holding the ``yq`` binary.

    Returns:
        Parent directory of ``yq``, to prepend to ``PATH``.

    Raises:
        RuntimeError: When ``yq`` is not installed.
    """
    yq = shutil.which('yq')
    if yq is None:
        raise RuntimeError('yq is not installed')
    return str(Path(yq).parent)


def old_step(step_id: str = 'def') -> str:
    """Return one step of the former inline ``Read package definition`` script.

    Args:
        step_id: Id of the step to extract.

    Returns:
        The step's ``run`` text at ``OLD_REV``.

    Raises:
        AssertionError: If the workflow holds no step with that id.
    """
    text = subprocess.run([
        'git', 'show', f'{OLD_REV}:.github/workflows/build-one-deb.yml'
    ],
                          cwd=ROOT,
                          check=True,
                          capture_output=True,
                          text=True).stdout
    workflow = as_mapping(load_yaml(text))
    for step in iter_steps(workflow):
        if step.get('id') == step_id:
            return as_str(step['run'])
    raise AssertionError(f'step {step_id} not found')


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
    _ = manifest.write_text(MANIFEST)
    out = tmp / f'{script.stem}-{pkg}.out'
    _ = out.write_text('')
    _ = subprocess.run(['bash', str(script)],
                       env={
                           'PATH': f'{_yq_dir()}:/usr/bin:/bin',
                           'MAN': str(manifest),
                           'PKG': pkg,
                           'ARCH': 'amd64',
                           'GITHUB_OUTPUT': str(out),
                           'SECRETVAL_GITHUB_TOKEN': 'tok-123'
                       },
                       check=True,
                       capture_output=True,
                       text=True)
    return out.read_text()


@pytest.mark.parametrize('pkg', ['demo', 'plain'])
def test_matches_inline_step(tmp_path: Path, pkg: str) -> None:
    """The extracted script writes the same outputs as the old step.

    Args:
        tmp_path: Pytest temporary directory.
        pkg: Package name passed to both scripts.
    """
    old = tmp_path / 'old.sh'
    _ = old.write_text('set -euo pipefail\n' + old_step())
    assert run(SCRIPT, tmp_path, pkg) == run(old, tmp_path, pkg)


def test_signs_flag_and_secret_resolved(tmp_path: Path) -> None:
    """A kernel-signing build is flagged and its secret env is resolved.

    Args:
        tmp_path: Pytest temporary directory.
    """
    out = run(SCRIPT, tmp_path, 'demo')
    assert 'signs=yes' in out
    assert 'GH_TOKEN=tok-123' in out
    assert 'deps=libfoo libbar' in out


def test_a_path_without_yq_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATH holding no yq raises the named error.

    Args:
        tmp_path: Empty directory used as the whole PATH.
        monkeypatch: Environment patcher.
    """
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(RuntimeError, match='yq is not installed'):
        _ = _yq_dir()


def test_an_unknown_step_id_is_reported() -> None:
    """The extractor fails on a step id the old workflow does not carry."""
    with pytest.raises(AssertionError, match='step no-such-step not found'):
        _ = old_step('no-such-step')
