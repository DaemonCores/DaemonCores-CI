"""Unit tests for ``upstream-track/track.sh``.

The GitHub API is replaced by a directory tree served through ``file://``: a
release is the JSON file ``repos/<owner>/<repo>/releases/latest``.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'upstream-track' / 'track.sh'
DIGEST = 'a' * 64
MANIFEST = """packages:
  - name: tool
    track:
      TOOL_VER:
        repo: acme/tool
        digests:
          TOOL_SHA256: "tool-{version}.tar.xz"
  - name: lib
    track:
      LIB_VER:
        repo: acme/lib
        strip-v: true
  - name: plain
"""


def _release(root: Path, repo: str, tag: str, assets: dict[str, str]) -> None:
    """Write the ``releases/latest`` document of ``repo``."""
    path = root / 'repos' / repo / 'releases'
    path.mkdir(parents=True, exist_ok=True)
    body = {
        'tag_name': tag,
        'assets': [{
            'name': name,
            'digest': digest
        } for name, digest in assets.items()]
    }
    (path / 'latest').write_text(json.dumps(body))


@pytest.fixture(name='api')
def fixture_api(tmp_path: Path) -> Path:
    """API tree with a manifest and one release per tracked repository."""
    (tmp_path / 'packages.yml').write_text(MANIFEST)
    _release(tmp_path, 'acme/tool', 'v2.0',
             {'tool-2.0.tar.xz': f'sha256:{DIGEST}'})
    _release(tmp_path, 'acme/lib', 'v2026.4', {})
    return tmp_path


def _run(api: Path, mode: str, **extra: str) -> subprocess.CompletedProcess[str]:
    """Run the script in ``mode`` against the file:// API tree."""
    env = {
        **os.environ,
        'MAN': str(api / 'packages.yml'),
        'GITHUB_API_URL': f'file://{api}',
        'GITHUB_OUTPUT': str(api / 'out'),
        **extra
    }
    return subprocess.run(['bash', str(SCRIPT), mode],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=60)


def _env_output(api: Path) -> list[str]:
    """Return the lines of the ``env`` step output."""
    text = (api / 'out').read_text().splitlines()
    return text[1:text.index('TRACK_ENV_EOF')]


def test_resolve_sets_version_and_digest(api: Path) -> None:
    """The latest tag and the asset digest replace the base values."""
    result = _run(api, 'resolve', PKG='tool', BASE_ENV='CARGO_HOME=/x')
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == [
        'CARGO_HOME=/x', 'TOOL_VER=v2.0', f'TOOL_SHA256={DIGEST}'
    ]


def test_resolve_strips_leading_v_on_request(api: Path) -> None:
    """``strip-v`` publishes the tag without its ``v``."""
    result = _run(api, 'resolve', PKG='lib', BASE_ENV='')
    assert result.returncode == 0, result.stderr
    assert _env_output(api) == ['LIB_VER=2026.4']


def test_resolve_replaces_a_base_value(api: Path) -> None:
    """A key already in the base env is replaced, not duplicated."""
    _run(api, 'resolve', PKG='lib', BASE_ENV='LIB_VER=old\nOTHER=1')
    assert _env_output(api) == ['OTHER=1', 'LIB_VER=2026.4']


def test_resolve_leaves_unresolved_entry_unset(api: Path) -> None:
    """A missing digest leaves the whole entry unset and warns."""
    _release(api, 'acme/tool', 'v2.0', {})
    result = _run(api, 'resolve', PKG='tool', BASE_ENV='A=1')
    assert result.returncode == 0
    assert _env_output(api) == ['A=1']
    assert '::warning::' in result.stderr


def test_resolve_strict_fails_when_unresolved(api: Path) -> None:
    """``STRICT=true`` turns an unresolved entry into a failure."""
    (api / 'repos' / 'acme' / 'lib' / 'releases' / 'latest').unlink()
    result = _run(api, 'resolve', PKG='lib', BASE_ENV='', STRICT='true')
    assert result.returncode == 1


def _published(api: Path, debs: dict[str, list[str]]) -> str:
    """Write a hashes.json and return its base URL."""
    cache = api / 'pages' / 'debs-cache'
    cache.mkdir(parents=True)
    (cache / 'hashes.json').write_text(
        json.dumps({k: {
            'hash': 'h',
            'debs': v
        } for k, v in debs.items()}))
    return f'file://{api}/pages'


def test_check_reports_unpublished_release(api: Path) -> None:
    """A latest release missing from the published debs is behind."""
    url = _published(api, {
        'tool-amd64': ['tool_1.0_amd64.deb'],
        'lib-amd64': ['lib_2026.4_amd64.deb']
    })
    result = _run(api, 'check', PAGES_URL=url, STRICT='true')
    assert result.returncode == 1
    assert 'tool TOOL_VER: upstream v2.0 is not published' in result.stdout
    assert 'lib LIB_VER: v2026.4 is published' in result.stdout


def test_check_passes_when_current(api: Path) -> None:
    """Every tracked release published means a clean strict check."""
    url = _published(api, {
        'tool-amd64': ['tool_2.0_amd64.deb'],
        'lib-amd64': ['lib_2026.4_amd64.deb']
    })
    assert _run(api, 'check', PAGES_URL=url, STRICT='true').returncode == 0
