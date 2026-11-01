"""``build-deb.sh`` keeps the last-good deb when there is nothing to build."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'build-deb' / 'build-deb.sh'
PRIOR_HASH = 'prior-hash'
DEB = 'kernel_7.2.14-1_amd64.deb'


def _pages(tmp_path: Path) -> Path:
    """Publish a one-deb APT cache for ``kernel-amd64`` as a file tree.

    Args:
        tmp_path: Scratch directory.

    Returns:
        The Pages root.
    """
    pages = tmp_path / 'pages'
    pkg = tmp_path / 'pkg' / 'DEBIAN'
    pkg.mkdir(parents=True)
    (pkg / 'control').write_text(
        'Package: kernel\nVersion: 7.2.14-1\nArchitecture: amd64\n'
        'Maintainer: ci <ci@example.invalid>\nDescription: test\n'
    )
    (pages / 'debs-cache').mkdir(parents=True)
    subprocess.run(
        ['dpkg-deb', '--build', str(pkg.parent),
         str(pages / 'debs-cache' / DEB)],
        check=True, capture_output=True
    )
    (pages / 'debs-cache' / 'hashes.json').write_text(
        json.dumps({'kernel-amd64': {'hash': PRIOR_HASH, 'debs': [DEB]}})
    )
    return pages


def _run(tmp_path: Path, skip: str, build: str) -> tuple[
        subprocess.CompletedProcess[str], list[str], Path]:
    """Run build-deb.sh against the fixture Pages.

    Args:
        tmp_path: Scratch directory.
        skip: Value of ``SKIP_BUILD``.
        build: Build script of the package.

    Returns:
        The process, the lines of the step output and the debs directory.
    """
    pages = _pages(tmp_path)
    debs = tmp_path / 'debs'
    debs.mkdir()
    out = tmp_path / 'out'
    out.touch()
    env = {
        'PATH': '/usr/bin:/bin', 'PACKAGE': 'kernel', 'ARCH': 'amd64',
        'BUILD': build, 'PAGES_URL': f'file://{pages}',
        'GITHUB_OUTPUT': str(out), 'GITHUB_WORKSPACE': str(tmp_path),
        'DEBS_DIR': str(debs), 'SKIP_BUILD': skip,
    }
    done = subprocess.run(
        ['bash', str(SCRIPT)], env=env, capture_output=True, text=True
    )
    return done, out.read_text().splitlines(), debs


def test_nothing_to_build_keeps_the_last_good_deb(tmp_path: Path) -> None:
    """A skip restores the published deb and does not flag a failure.

    Args:
        tmp_path: Scratch directory.
    """
    done, out, debs = _run(tmp_path, 'true', 'exit 1')
    assert done.returncode == 0, done.stderr
    assert 'skipped=true' in out
    assert 'build_failed=true' not in out
    assert (debs / DEB).is_file()
    meta = json.loads((debs / '.meta' / 'kernel-amd64.json').read_text())
    assert meta == {'hash': PRIOR_HASH, 'skipped': True, 'debs': [DEB]}


def test_without_skip_a_failing_build_is_still_a_failure(
        tmp_path: Path) -> None:
    """Only an explicit skip avoids the build; a failing build stays flagged.

    Args:
        tmp_path: Scratch directory.
    """
    done, out, _ = _run(tmp_path, 'false', 'exit 1')
    assert done.returncode == 0, done.stderr
    assert 'build_failed=true' in out
