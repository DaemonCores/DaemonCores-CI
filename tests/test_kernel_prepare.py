"""Tests for the kernel deb lookup in scripts/kernel_prepare.sh."""

import subprocess
from pathlib import Path

SCRIPT = Path(__file__
              ).resolve().parent.parent / 'scripts' / 'kernel_prepare.sh'

_DRIVER = r'''
set -euo pipefail
source "$1"
apt-get() { :; }
dpkg-deb() {
  case "$3" in
    Version) echo 7.2.14-1;;
    Package) echo "${STUB_PACKAGE:-daemoncores-kernel-7.2.14}";;
    Provides) echo "${STUB_PROVIDES:-}";;
  esac
}
kernel_fetch_source() { echo "$COMMON_DEB"; echo "KVER=$KVER" >&2; exit 0; }
DEB_ARCH=amd64
kernel_prepare
'''


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run kernel_prepare with stubbed apt/dpkg until the source download.

    Args:
        tmp_path: Scratch directory.
        **env: Extra environment variables.

    Returns:
        The finished process.
    """
    return subprocess.run(['bash', '-c', _DRIVER, 'driver',
                           str(SCRIPT)],
                          env={
                              'PATH': '/usr/bin:/bin',
                              **env
                          },
                          cwd=tmp_path,
                          capture_output=True,
                          text=True,
                          check=False)


def test_deb_found_in_debs_dir(tmp_path: Path) -> None:
    """The kernel deb is found in DEBS_DIR even when /tmp/prior does not exist.

    Args:
        tmp_path: Scratch directory.
    """
    debs = tmp_path / 'debs'
    debs.mkdir()
    deb = debs / 'linux-image-common_7.2.14-1_amd64.deb'
    deb.write_bytes(b'')
    done = _run(tmp_path, DEBS_DIR=str(debs))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == str(deb)
    assert 'KVER=7.2.14' in done.stderr


def test_kver_comes_from_the_versioned_provides(tmp_path: Path) -> None:
    """A versionless kernel deb names its release through Provides.

    Args:
        tmp_path: Scratch directory.
    """
    debs = tmp_path / 'debs'
    debs.mkdir()
    (debs / 'linux-image-common_7.2.14-1_amd64.deb').write_bytes(b'')
    done = _run(
        tmp_path,
        DEBS_DIR=str(debs),
        STUB_PACKAGE='linux-image-ia-x64v2',
        STUB_PROVIDES='linux-image-minimal-ia-x64v2, '
        'linux-image-7.2.14-ia (= 7.2.14-1), daemoncores-kernel'
    )
    assert done.returncode == 0, done.stderr
    assert 'KVER=7.2.14-ia' in done.stderr


def test_missing_deb_is_an_error(tmp_path: Path) -> None:
    """Without a kernel deb the function fails with a clear message.

    Args:
        tmp_path: Scratch directory.
    """
    done = _run(tmp_path, DEBS_DIR=str(tmp_path / 'none'))
    assert done.returncode == 1
    assert 'linux-image-common deb not found' in done.stderr
