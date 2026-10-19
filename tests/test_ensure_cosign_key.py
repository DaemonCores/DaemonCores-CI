"""Unit tests for ``scripts/ensure-cosign-key.sh``.

Stub ``gh`` and ``cosign`` binaries stand in for GitHub and for key generation:
``gh secret list`` prints the names in ``EXISTING``, ``gh secret set`` stores
its stdin under ``<SECRETS_DIR>/<name>``.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'ensure-cosign-key.sh'
GH_STUB = """#!/bin/bash
if [ "$1 $2" = "secret list" ]; then
  printf '%s' "$EXISTING"
elif [ "$1 $2" = "secret set" ]; then
  [ -z "$GH_FAIL" ] || exit 1
  cat >"$SECRETS_DIR/$3"
fi
"""
COSIGN_STUB = """#!/bin/bash
echo "PRIVATE" >cosign.key
echo "PUBLIC" >cosign.pub
printf '%s' "$COSIGN_PASSWORD" >"$SECRETS_DIR/.password_seen"
"""


@pytest.fixture(name='env')
def fixture_env(tmp_path: Path) -> dict[str, str]:
    """Environment with stub gh and cosign first on PATH."""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, body in (('gh', GH_STUB), ('cosign', COSIGN_STUB)):
        stub = bindir / name
        stub.write_text(body)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    secrets = tmp_path / 'secrets'
    secrets.mkdir()
    return {
        **os.environ,
        'PATH': f'{bindir}:{os.environ["PATH"]}',
        'SECRETS_DIR': str(secrets),
        'GH_TOKEN': 'token',
        'GITHUB_REPOSITORY': 'owner/repo',
        'GITHUB_OUTPUT': str(tmp_path / 'out'),
        'EXISTING': 'PAT_PKG\n',
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT)],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=60)


def test_generates_the_three_secrets_when_absent(env: dict[str, str]) -> None:
    """A missing key pair is generated and stored, password included."""
    result = _run(env)
    assert result.returncode == 0, result.stderr
    secrets = Path(env['SECRETS_DIR'])
    assert (secrets / 'COSIGN_PRIVATE_KEY').read_text().strip() == 'PRIVATE'
    assert (secrets / 'COSIGN_PUBLIC_KEY').read_text().strip() == 'PUBLIC'
    password = (secrets / 'COSIGN_PASSWORD').read_text()
    assert len(password) == 48
    assert (secrets / '.password_seen').read_text() == password
    assert password not in result.stdout + result.stderr
    assert 'created=yes' in Path(env['GITHUB_OUTPUT']).read_text()


def test_existing_key_is_left_alone(env: dict[str, str]) -> None:
    """An existing key is never regenerated: hosts pin its public half."""
    env['EXISTING'] = 'PAT_PKG\nCOSIGN_PRIVATE_KEY\n'
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert not list(Path(env['SECRETS_DIR']).iterdir())
    assert 'created=no' in Path(env['GITHUB_OUTPUT']).read_text()


def test_without_token_falls_back_to_keyless(env: dict[str, str]) -> None:
    """No PAT_SECRETS: warn, create nothing, succeed."""
    env['GH_TOKEN'] = ''
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert '::warning::' in result.stdout
    assert not list(Path(env['SECRETS_DIR']).iterdir())
    assert 'created=no' in Path(env['GITHUB_OUTPUT']).read_text()


def test_write_failure_is_an_error(env: dict[str, str]) -> None:
    """A token that cannot write secrets fails loudly."""
    env['GH_FAIL'] = '1'
    result = _run(env)
    assert result.returncode != 0
    assert 'secrets:write' in result.stderr
