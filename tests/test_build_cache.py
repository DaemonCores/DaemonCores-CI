"""``build-deb.sh`` never builds the same content hash twice."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'build-deb' / 'build-deb.sh'
BUILD = (
    'mkdir -p /tmp/p$$/DEBIAN && printf "Package: tool\\nVersion: 1\\n'
    'Architecture: amd64\\nMaintainer: ci <ci@example.invalid>\\n'
    'Description: t\\n" > /tmp/p$$/DEBIAN/control && '
    'dpkg-deb --build /tmp/p$$ "${DEBS_DIR}/tool_1_amd64.deb" >/dev/null'
)


def _run(tmp_path: Path, run: str, build: str, **extra: str) -> tuple[
    subprocess.CompletedProcess[str], list[str], Path
]:
    """Run build-deb.sh on an empty published site.

    Args:
        tmp_path: Scratch directory.
        run: Name of this run (its own debs directory and output file).
        build: Build script of the package.
        **extra: Extra environment variables.

    Returns:
        The process, the lines of the step output and the debs directory.
    """
    pages = tmp_path / 'pages'
    (pages / 'debs-cache').mkdir(parents=True, exist_ok=True)
    (pages / 'debs-cache' / 'hashes.json').write_text('{}')
    debs = tmp_path / f'debs-{run}'
    debs.mkdir()
    out = tmp_path / f'out-{run}'
    out.touch()
    env = {
        'PATH': '/usr/bin:/bin', 'PACKAGE': 'tool', 'ARCH': 'amd64',
        'BUILD': build, 'PAGES_URL': f'file://{pages}',
        'GITHUB_OUTPUT': str(out), 'GITHUB_WORKSPACE': str(tmp_path),
        'DEBS_DIR': str(debs), **extra,
    }
    done = subprocess.run(['bash', str(SCRIPT)], env=env,
                          capture_output=True, text=True, check=False)
    return done, out.read_text().splitlines(), debs


def test_a_second_run_reuses_the_build_of_the_same_hash(
    tmp_path: Path
) -> None:
    """The deb built once is restored for the same content, not rebuilt.

    Args:
        tmp_path: Scratch directory.
    """
    cache = tmp_path / 'cache'
    first, out1, _ = _run(tmp_path, '1', BUILD, DEB_CACHE_DIR=str(cache))
    assert first.returncode == 0, first.stderr
    assert 'skipped=false' in out1
    assert json.loads((cache / 'debs.json').read_text()) == ['tool_1_amd64.deb']
    # The first run never published: the site is still empty. The failing
    # build proves the second run does not build.
    second, out2, debs = _run(tmp_path, '2', 'exit 1', DEB_CACHE_DIR=str(cache))
    assert second.returncode == 0, second.stderr
    assert 'from_build_cache=true' in out2
    assert 'build_failed=true' not in out2
    assert (debs / 'tool_1_amd64.deb').is_file()
    meta = json.loads((debs / '.meta' / 'tool-amd64.json').read_text())
    assert meta['skipped'] is False


def test_an_empty_cache_directory_builds(tmp_path: Path) -> None:
    """A miss of the hash-addressed key restores nothing: the package builds.

    The key (package, arch, content hash) lives in the workflow: another
    content means another key, hence an empty directory here.

    Args:
        tmp_path: Scratch directory.
    """
    done, out, debs = _run(tmp_path, '1', BUILD,
                           DEB_CACHE_DIR=str(tmp_path / 'empty'))
    assert done.returncode == 0, done.stderr
    assert 'from_build_cache=true' not in out
    assert 'skipped=false' in out
    assert (debs / 'tool_1_amd64.deb').is_file()


def test_hash_only_computes_the_key_and_builds_nothing(tmp_path: Path) -> None:
    """The key step outputs the hash and stops: no build, no metadata.

    Args:
        tmp_path: Scratch directory.
    """
    marker = tmp_path / 'ran'
    done, out, debs = _run(tmp_path, '1', f'touch {marker}', HASH_ONLY='true')
    assert done.returncode == 0, done.stderr
    assert any(line.startswith('hash=') for line in out)
    assert not marker.exists()
    assert not (debs / '.meta').exists()
