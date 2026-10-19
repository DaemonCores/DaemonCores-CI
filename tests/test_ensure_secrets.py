"""Unit tests for ``scripts/ensure-secrets.sh``.

Stub ``gh`` and ``cosign`` binaries stand in for GitHub and for key generation:
``gh secret list`` prints the names in ``EXISTING``, ``gh secret set`` stores its
stdin under ``<SECRETS_DIR>/<name>``.  ``openssl`` and ``gpg`` are the real ones.
"""

import base64
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'ensure-secrets.sh'
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


def _run(env: dict[str, str],
         *groups: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT), *groups],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=120)


def _created(env: dict[str, str]) -> str:
    return Path(env['GITHUB_OUTPUT']).read_text().strip()


def test_cosign_generates_the_three_secrets(env: dict[str, str]) -> None:
    """A missing key pair is generated and stored, password included."""
    result = _run(env, 'cosign')
    assert result.returncode == 0, result.stderr
    secrets = Path(env['SECRETS_DIR'])
    assert (secrets / 'COSIGN_PRIVATE_KEY').read_text().strip() == 'PRIVATE'
    assert (secrets / 'COSIGN_PUBLIC_KEY').read_text().strip() == 'PUBLIC'
    password = (secrets / 'COSIGN_PASSWORD').read_text()
    assert len(password) == 48
    assert (secrets / '.password_seen').read_text() == password
    assert password not in result.stdout + result.stderr
    assert _created(env) == 'created=cosign'


def test_existing_secret_is_left_alone(env: dict[str, str]) -> None:
    """An existing key is never regenerated: hosts pin its public half."""
    env['EXISTING'] = 'PAT_PKG\nCOSIGN_PRIVATE_KEY\n'
    result = _run(env, 'cosign')
    assert result.returncode == 0, result.stderr
    assert not list(Path(env['SECRETS_DIR']).iterdir())
    assert _created(env) == 'created='


def test_secure_boot_pair_is_base64_and_matches(env: dict[str, str]) -> None:
    """The Secure Boot key and certificate are stored base64, as the build reads them."""
    result = _run(env, 'sb')
    assert result.returncode == 0, result.stderr
    secrets = Path(env['SECRETS_DIR'])
    key = base64.b64decode((secrets / 'SB_SIGNING_KEY').read_text())
    cert = base64.b64decode((secrets / 'SB_SIGNING_CERT').read_text())
    assert b'PRIVATE KEY' in key
    assert b'BEGIN CERTIFICATE' in cert
    modulus = [
        subprocess.run(cmd,
                       input=data,
                       check=True,
                       capture_output=True,
                       timeout=60).stdout for cmd, data in (
                           (['openssl', 'rsa', '-noout', '-modulus'], key),
                           (['openssl', 'x509', '-noout', '-modulus'], cert),
                       )
    ]
    assert modulus[0] == modulus[1]
    assert b'PRIVATE KEY' not in (result.stdout + result.stderr).encode()
    assert _created(env) == 'created=sb'


def test_module_signing_pair(env: dict[str, str]) -> None:
    """Module signing key and certificate are stored as plain PEM."""
    assert _run(env, 'modules').returncode == 0
    secrets = Path(env['SECRETS_DIR'])
    assert 'PRIVATE KEY' in (secrets / 'MODULE_SIGNING_KEY').read_text()
    assert 'BEGIN CERTIFICATE' in (secrets / 'MODULE_SIGNING_CERT').read_text()


def test_apt_key_goes_to_the_secret(env: dict[str, str]) -> None:
    """The APT group stores an armored private key."""
    result = _run(env, 'apt')
    assert result.returncode == 0, result.stderr
    secret = (Path(env['SECRETS_DIR']) / 'APT_GPG_KEY').read_text()
    assert 'BEGIN PGP PRIVATE KEY BLOCK' in secret
    assert _created(env) == 'created=apt'


def test_several_groups_report_only_what_was_created(
        env: dict[str, str]) -> None:
    """Groups already present are skipped, the others are created."""
    env['EXISTING'] = 'SB_SIGNING_KEY\n'
    assert _run(env, 'sb', 'modules').returncode == 0
    assert _created(env) == 'created=modules'
    assert not (Path(env['SECRETS_DIR']) / 'SB_SIGNING_CERT').exists()


def test_without_token_nothing_is_created(env: dict[str, str]) -> None:
    """No PAT_SECRETS: warn, create nothing, succeed."""
    env['GH_TOKEN'] = ''
    result = _run(env, 'cosign', 'sb')
    assert result.returncode == 0, result.stderr
    assert '::warning::' in result.stdout
    assert not list(Path(env['SECRETS_DIR']).iterdir())
    assert _created(env) == 'created='


def test_write_failure_is_an_error(env: dict[str, str]) -> None:
    """A token that cannot write secrets fails loudly."""
    env['GH_FAIL'] = '1'
    result = _run(env, 'sb')
    assert result.returncode != 0
    assert 'secrets:write' in result.stderr


def test_unknown_group_is_refused(env: dict[str, str]) -> None:
    """A typo in a group name is an error, not a silent no-op."""
    assert _run(env, 'nope').returncode != 0
