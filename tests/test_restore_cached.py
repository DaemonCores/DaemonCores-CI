"""Tests for ``restore_cached`` in build-deb/content-hash.sh."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / 'build-deb' / 'content-hash.sh'

pytestmark = pytest.mark.skipif(
    not (
        shutil.which('dpkg-deb') and shutil.which('jq')
        and shutil.which('curl')
    ),
    reason='needs dpkg-deb, jq and curl'
)


def _deb(path: Path) -> None:
    """Build a small real deb at ``path``.

    Args:
        path: Destination file.
    """
    tree = path.parent / 'tree'
    (tree / 'DEBIAN').mkdir(parents=True)
    (tree / 'DEBIAN' / 'control').write_text(
        'Package: p\nVersion: 1\nArchitecture: all\nMaintainer: t <t@t>\n'
        'Description: t\n',
        encoding='utf-8'
    )
    (tree / 'f').write_bytes(os.urandom(60000))
    subprocess.run(['dpkg-deb', '--build',
                    str(tree), str(path)],
                   check=True,
                   capture_output=True)
    shutil.rmtree(tree)


def _restore(tmp: Path, name: str) -> subprocess.CompletedProcess:
    """Run restore_cached for one deb served from a file:// site.

    Args:
        tmp: Scratch directory holding ``site/debs-cache``.
        name: Deb file name.

    Returns:
        The finished process.
    """
    out = tmp / 'out'
    out.mkdir(exist_ok=True)
    script = f'. "{LIB}"; DEBS_DIR="{out}"; ' \
             f'restore_cached "file://{tmp / "site"}" \'["{name}"]\''
    return subprocess.run(['bash', '-c', script],
                          capture_output=True,
                          text=True,
                          check=False)


def test_sound_deb_is_reused(tmp_path: Path) -> None:
    """A deb that unpacks is restored."""
    cache = tmp_path / 'site' / 'debs-cache'
    cache.mkdir(parents=True)
    _deb(cache / 'p_1_all.deb')
    res = _restore(tmp_path, 'p_1_all.deb')
    assert res.returncode == 0, res.stderr
    assert (tmp_path / 'out' / 'p_1_all.deb').exists()


def test_corrupt_deb_is_a_cache_miss(tmp_path: Path) -> None:
    """A published deb that does not unpack is discarded, forcing a rebuild."""
    cache = tmp_path / 'site' / 'debs-cache'
    cache.mkdir(parents=True)
    _deb(cache / 'p_1_all.deb')
    data = bytearray((cache / 'p_1_all.deb').read_bytes())
    data[len(data) // 2] ^= 0xFF
    (cache / 'p_1_all.deb').write_bytes(bytes(data))
    res = _restore(tmp_path, 'p_1_all.deb')
    assert res.returncode == 1
    assert 'does not unpack' in res.stderr
    assert not (tmp_path / 'out' / 'p_1_all.deb').exists()
