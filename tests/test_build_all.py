"""Unit tests for ``build-deb/build-all.sh`` with stubbed sibling scripts.

The real script is copied next to stub ``pkg-def.sh``, ``track.sh`` and
``build-deb.sh``: the stubs append what they receive to a log and write the
outputs the real ones would, so the test covers the orchestration only (order,
results, dependency closure, exit status), not the builds themselves.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which('yq') is None or shutil.which('jq') is None,
    reason='yq and jq are required by build-all.sh')

MANIFEST = """
packages:
  - name: lib
    build: echo lib
  - name: app
    deps: [lib]
    deb-glob: app_*
    build: echo app
  - name: broken
    build: echo broken
  - name: gone
    build: echo gone
"""

PKG_DEF = """#!/bin/bash
{
  echo "upstream="
  echo "deb_glob=${PKG}_*"
  echo "sources="
  echo "signs=no"
  echo "env="
  echo "deps=$(yq ".packages[] | select(.name == \\"${PKG}\\") | (.deps // []) | join(\\" \\")" "${MAN}")"
  echo "build=echo"
} >>"${GITHUB_OUTPUT}"
"""

TRACK = """#!/bin/bash
echo "env=" >>"${GITHUB_OUTPUT}"
"""

BUILD_DEB = """#!/bin/bash
echo "${PACKAGE} deps=${DEP_DEBS//$'\\n'/,}" >>"${STUB_LOG}"
case "${PACKAGE}" in
  lib) echo "skipped=true" >>"${GITHUB_OUTPUT}" ;;
  app) echo "skipped=false" >>"${GITHUB_OUTPUT}" ;;
  broken) echo "build_failed=true" >>"${GITHUB_OUTPUT}" ;;
  gone) exit 1 ;;
esac
"""


def _sandbox(tmp_path: Path) -> Path:
    """Lay out the script, the stubs and the manifest.

    Args:
        tmp_path: Scratch directory.

    Returns:
        The sandbox root.
    """
    (tmp_path / 'build-deb').mkdir()
    for name in ('build-all.sh', 'plan.py'):
        shutil.copy(ROOT / 'build-deb' / name, tmp_path / 'build-deb' / name)
    for rel, body in (('pkg-def/pkg-def.sh', PKG_DEF),
                      ('upstream-track/track.sh', TRACK),
                      ('build-deb/build-deb.sh', BUILD_DEB)):
        path = tmp_path / rel
        path.parent.mkdir(exist_ok=True)
        path.write_text(body, encoding='utf-8')
    (tmp_path / 'm.yml').write_text(MANIFEST, encoding='utf-8')
    (tmp_path / 'debs').mkdir()
    (tmp_path / 'debs' / 'lib_1.deb').write_bytes(b'lib')
    return tmp_path


def _run(root: Path) -> subprocess.CompletedProcess:
    """Run build-all.sh in the sandbox.

    Args:
        root: Sandbox root.

    Returns:
        The completed process.
    """
    env = dict(os.environ,
               DEBS_DIR=str(root / 'debs'),
               BUILD_DIR=str(root / 'build'),
               TMPDIR=str(root / 'tmp'),
               STUB_LOG=str(root / 'log'),
               GITHUB_OUTPUT=str(root / 'out'),
               GITHUB_API_URL='http://127.0.0.1:9',
               GITHUB_REPOSITORY='o/r',
               GITHUB_SERVER_URL='http://127.0.0.1:9',
               GITHUB_RUN_ID='1',
               GITHUB_TOKEN='t')
    (root / 'build').mkdir(exist_ok=True)
    (root / 'tmp').mkdir(exist_ok=True)
    return subprocess.run(
        ['bash', str(root / 'build-deb' / 'build-all.sh'),
         str(root / 'm.yml'), str(root / 'm.yml')],
        env=env,
        capture_output=True,
        text=True,
        check=False)


def test_results_order_and_exit_status(tmp_path: Path) -> None:
    """Each package gets its result; a kept or failed one fails the job.

    Args:
        tmp_path: Scratch directory.
    """
    root = _sandbox(tmp_path)
    done = _run(root)
    out = done.stdout
    assert 'result lib: reused' in out, done.stderr
    assert 'result app: built' in out
    assert 'result broken: kept' in out
    assert 'result gone: failed' in out
    assert done.returncode == 1
    assert out.index('result lib') < out.index('result app')
    assert 'any_built=true' in (root / 'out').read_text(encoding='utf-8')


def test_dependency_debs_are_handed_to_the_build(tmp_path: Path) -> None:
    """The dependency closure is read from the debs directory.

    Args:
        tmp_path: Scratch directory.
    """
    root = _sandbox(tmp_path)
    _run(root)
    log = (root / 'log').read_text(encoding='utf-8')
    assert f'app deps={root}/debs/lib_1.deb' in log
    assert 'lib deps=\n' in log


def test_missing_dependency_deb_fails_the_package(tmp_path: Path) -> None:
    """A dependency without a deb fails its dependent without building it.

    Args:
        tmp_path: Scratch directory.
    """
    root = _sandbox(tmp_path)
    (root / 'debs' / 'lib_1.deb').unlink()
    done = _run(root)
    assert 'result app: failed' in done.stdout
    assert 'app deps' not in (root / 'log').read_text(encoding='utf-8')
