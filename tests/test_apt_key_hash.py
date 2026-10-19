"""Unit tests for ``scripts/apt-key-hash.sh``.

The secret key comes from ``rotate-apt-key.sh`` (stub ``gh`` captures it); the
published key is a ``file://`` URL.
"""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HASH = ROOT / 'scripts' / 'apt-key-hash.sh'
ROTATE = ROOT / 'scripts' / 'rotate-apt-key.sh'
GH_STUB = """#!/bin/bash
cat >"$GH_STDIN"
"""


def _gpg_home() -> tuple[Path, dict[str, str]]:
    """Create a scratch GnuPG home.

    The path is kept short because gpg-agent sockets cannot exceed about
    100 characters.

    Returns:
        The home directory and an environment pointing GNUPGHOME at it.
    """
    home = Path(tempfile.mkdtemp(dir='/tmp'))
    return home, {**os.environ, 'GNUPGHOME': str(home)}


def _public_of(secret: str) -> str:
    """Derive the public key of an exported secret key.

    Args:
        secret: Armored private key.

    Returns:
        The armored public key.
    """
    home, genv = _gpg_home()
    try:
        imp = subprocess.run([
            'gpg', '--batch', '--quiet', '--pinentry-mode', 'loopback',
            '--passphrase', '', '--import'
        ],
                             input=secret,
                             text=True,
                             check=False,
                             env=genv,
                             capture_output=True)
        assert imp.returncode == 0, imp.stderr
        return subprocess.run(['gpg', '--batch', '--armor', '--export'],
                              check=True,
                              env=genv,
                              capture_output=True,
                              text=True).stdout
    finally:
        subprocess.run(['gpgconf', '--kill', 'gpg-agent'],
                       env=genv,
                       check=False)
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(name='secret', scope='module')
def fixture_secret(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Generate an armored private key through ``rotate-apt-key.sh``.

    Args:
        tmp_path_factory: Pytest factory for the module-scoped scratch dir.

    Returns:
        The armored private key captured from the stub ``gh``.
    """
    base = tmp_path_factory.mktemp('rotate')
    gh = base / 'gh'
    gh.write_text(GH_STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        'PATH': f'{base}:{os.environ["PATH"]}',
        'GH_STDIN': str(base / 'stdin'),
    }
    done = subprocess.run([str(ROTATE), 'rotate'],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    return (base / 'stdin').read_text()


def _run(secret: str, url: str) -> subprocess.CompletedProcess[str]:
    """Run ``apt-key-hash.sh`` against a key URL.

    Args:
        secret: Armored private key passed as APT_GPG_KEY.
        url: Location of the published public key.

    Returns:
        The completed process.
    """
    return subprocess.run([str(HASH)],
                          check=False,
                          capture_output=True,
                          text=True,
                          timeout=120,
                          env={
                              **os.environ, 'APT_GPG_KEY': secret,
                              'KEY_URL': url,
                              'WAIT_ATTEMPTS': '2',
                              'WAIT_SECONDS': '0'
                          })


def test_hash_of_the_served_key(secret: str, tmp_path: Path) -> None:
    """A served key matching the secret yields the SHA-256 of the file.

    Args:
        secret: Armored private key fixture.
        tmp_path: Pytest scratch directory.
    """
    served = tmp_path / 'gpg.key'
    served.write_text(_public_of(secret))
    result = _run(secret, served.as_uri())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hashlib.sha256(served.read_bytes()
                                                   ).hexdigest()
    assert 'PRIVATE KEY' not in result.stdout + result.stderr


def test_a_stale_published_key_is_refused(secret: str, tmp_path: Path) -> None:
    """A served key from another pair never produces a hash.

    Args:
        secret: Armored private key fixture.
        tmp_path: Pytest scratch directory.
    """
    other = tmp_path / 'other'
    other.mkdir()
    gh = other / 'gh'
    gh.write_text(GH_STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        'PATH': f'{other}:{os.environ["PATH"]}',
        'GH_STDIN': str(other / 'stdin'),
    }
    assert subprocess.run([str(ROTATE), 'rotate'],
                          check=False,
                          capture_output=True,
                          env=env,
                          timeout=120).returncode == 0
    served = tmp_path / 'gpg.key'
    served.write_text(_public_of((other / 'stdin').read_text()))
    result = _run(secret, served.as_uri())
    assert result.returncode != 0
    assert result.stdout == ''
    assert 'does not match' in result.stderr


def test_missing_secret_is_an_error(tmp_path: Path) -> None:
    """An empty APT_GPG_KEY fails up front.

    Args:
        tmp_path: Pytest scratch directory.
    """
    result = _run('', (tmp_path / 'x').as_uri())
    assert result.returncode != 0
    assert 'APT_GPG_KEY' in result.stderr
