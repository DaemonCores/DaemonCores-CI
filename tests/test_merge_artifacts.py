"""Tests for bootc-debs-end/merge-artifacts.sh."""
import os
import subprocess
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve(
).parent.parent / 'bootc-debs-end' / ('merge-artifacts.sh')


def _deb(path: Path, payload: bytes) -> None:
    """Build a real deb whose content depends on the payload.

    Args:
        path: Destination file.
        payload: Bytes stored in the package.
    """
    root = path.parent / (path.name + '.tree')
    (root / 'DEBIAN').mkdir(parents=True)
    (root / 'DEBIAN' / 'control').write_text(
        'Package: p\nVersion: 1\nArchitecture: all\nMaintainer: t <t@t>\n'
        'Description: t\n',
        encoding='utf-8'
    )
    (root / 'f').write_bytes(payload)
    subprocess.run(['dpkg-deb', '--build',
                    str(root), str(path)],
                   check=True,
                   capture_output=True)
    shutil.rmtree(root)


def _run(tmp: Path) -> subprocess.CompletedProcess:
    """Run the script on tmp/debs-in.

    Args:
        tmp: Scratch directory.

    Returns:
        The finished process.
    """
    env = dict(os.environ, DEBS_IN=str(tmp / 'in'), DEBS_DIR=str(tmp / 'out'))
    return subprocess.run(['bash', str(SCRIPT)],
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)


@pytest.mark.skipif(not shutil.which('dpkg-deb'), reason='needs dpkg-deb')
def test_same_name_builds_are_not_mixed(tmp_path: Path):
    """Two builds of one arch-all deb give exactly one intact copy."""
    for arch, payload in (('amd64', b'A' * 5000), ('arm64', b'B' * 5000)):
        d = tmp_path / 'in' / f'debs-p-{arch}'
        d.mkdir(parents=True)
        _deb(d / 'p_1_all.deb', payload)
        (d / f'{arch}.json').write_text('{}', encoding='utf-8')
    res = _run(tmp_path)
    assert res.returncode == 0, res.stderr
    assert 'duplicate p_1_all.deb' in res.stdout
    kept = (tmp_path / 'out' / 'p_1_all.deb').read_bytes()
    assert kept == (tmp_path / 'in' / 'debs-p-amd64' /
                    'p_1_all.deb').read_bytes()
    assert (tmp_path / 'out' / 'amd64.json').exists()
    assert (tmp_path / 'out' / 'arm64.json').exists()


@pytest.mark.skipif(not shutil.which('dpkg-deb'), reason='needs dpkg-deb')
def test_corrupt_deb_is_refused(tmp_path: Path):
    """A deb that does not unpack fails the merge."""
    d = tmp_path / 'in' / 'debs-p-amd64'
    d.mkdir(parents=True)
    _deb(d / 'p_1_all.deb', os.urandom(60000))
    data = bytearray((d / 'p_1_all.deb').read_bytes())
    data[len(data) // 2] ^= 0xFF
    (d / 'p_1_all.deb').write_bytes(bytes(data))
    res = _run(tmp_path)
    assert res.returncode != 0
    assert 'does not unpack' in res.stderr
