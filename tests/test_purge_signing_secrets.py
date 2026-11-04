"""End-of-run purge of the signing material (P-44/P-45).

``purge-signing-secrets.sh`` unsets every signing secret variable in its own
shell and shreds the private-key artifacts (``*.key``, ``cosign.key``,
``sb.key``, ``mod.pem``, ``signing_key.pem``) found under its sweep roots,
leaving public certificates (.crt, *.x509) untouched.
"""

import os
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / 'scripts' / 'purge-signing-secrets.sh'
)


def run_purge(root: Path, env: dict[str,
                                    str]) -> subprocess.CompletedProcess[str]:
    """Run the purge script against ``root`` as its only sweep argument.

    Args:
        root: directory swept by the purge.
        env: full environment handed to the script.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ['bash', str(SCRIPT), str(root)],
        env={
            **env,
            'PATH': os.environ['PATH'],
        },
        capture_output=True,
        text=True,
        check=False,
    )


def temp_env() -> dict[str, str]:
    """A minimal environment (HOME only) for isolated purge runs.

    Returns:
        The environment mapping.
    """
    return {'HOME': os.environ.get('HOME', '/tmp')}


def test_private_key_artifacts_are_purged(tmp_path: Path) -> None:
    """Every private key artifact under the sweep root is removed.

    Args:
        tmp_path: clean sweep root built by pytest.
    """
    for name in ('signing_key.pem', 'sb.key', 'mod.pem', 'v7.key'):
        (tmp_path / name).write_text('PRIVATE')
    (tmp_path / 'sb.crt').write_text('public cert stays')
    result = run_purge(tmp_path, temp_env())
    assert result.returncode == 0, result.stderr
    for name in ('signing_key.pem', 'sb.key', 'mod.pem', 'v7.key'):
        assert not (tmp_path / name).exists(), name
    assert (tmp_path / 'sb.crt').read_text() == 'public cert stays'


def test_signing_env_never_breaks_the_purge(tmp_path: Path) -> None:
    """The purge runs clean with every signing variable set in its env.

    The script runs under ``set -u`` and unsets them itself: any variable it
    failed to unset before the end would make its own final state crash.

    Args:
        tmp_path: clean sweep root built by pytest.
    """
    env = {
        **temp_env(),
        'PATH': os.environ['PATH'],
        'COSIGN_PRIVATE_KEY': 'k',
        'COSIGN_PASSWORD': 'p',
        'APT_GPG_KEY': 'g',
        'MODULE_SIGNING_KEY': 'm',
    }
    result = run_purge(tmp_path, env)
    assert result.returncode == 0, result.stderr


def test_sweep_skips_missing_roots(tmp_path: Path) -> None:
    """A non-existent sweep root is skipped, the purge still exits 0.

    Args:
        tmp_path: directory the absent sweep root is built under.
    """
    result = subprocess.run(
        ['bash', str(SCRIPT), str(tmp_path / 'absent')],
        env=temp_env() | {'PATH': os.environ['PATH']},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
