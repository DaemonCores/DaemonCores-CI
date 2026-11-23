"""``deb-plan/plan.sh`` skips what is unchanged before any build job starts."""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'deb-plan' / 'plan.sh'
HASH = ROOT / 'build-deb' / 'content-hash.sh'
MANIFEST = """packages:
  - name: Alpha
    sources: [alpha]
    build: echo alpha
  - name: beta
    sources: [beta]
    deps: [Alpha]
    build: echo beta
  - name: gamma
    sources: [gamma]
    build: echo gamma
"""


def _workspace(tmp_path: Path) -> Path:
    """Create the workspace: manifest plus one source file per package.

    Args:
        tmp_path: Scratch directory.

    Returns:
        The workspace root (the working directory of the plan).
    """
    ws = tmp_path / 'ws'
    base = ws / 'workflows' / 'bootc-debs-builder'
    for name in ('alpha', 'beta', 'gamma'):
        (base / name).mkdir(parents=True)
        (base / name / 'f').write_text(name)
    (base / 'packages.yml').write_text(MANIFEST)
    return ws


def _hash_of(ws: Path, name: str, build: str, sources: str) -> str:
    """Compute the content hash build-deb.sh would compute.

    Args:
        ws: Workspace root.
        name: Package name.
        build: Build script text.
        sources: Workspace-relative source directory.

    Returns:
        The hex digest.
    """
    script = f'. {HASH}; content_hash 2>/dev/null'
    env = {
        **os.environ, 'PACKAGE': name,
        'ARCH': 'amd64',
        'SOURCES': sources,
        'UPSTREAM': '',
        'BUILD': build,
        'EXTRA_ENV': '',
        'EXTRA_ENV_CI': ''
    }
    done = subprocess.run(['bash', '-c', script],
                          cwd=ws,
                          env=env,
                          capture_output=True,
                          text=True,
                          check=True)
    return done.stdout.strip()


def _plan(ws: Path, pages: Path) -> tuple[dict[str, str], str]:
    """Run the plan on the three packages (one wave each).

    Args:
        ws: Workspace root.
        pages: Published tree served through file://.

    Returns:
        The step outputs and the stderr of the plan.
    """
    out = ws / 'out'
    out.write_text('')
    env = {
        **os.environ,
        'MAN': str(ws / 'workflows/bootc-debs-builder/packages.yml'),
        'ARCH': 'amd64',
        'WAVES': json.dumps({
            'wave0': ['Alpha', 'gamma'],
            'wave1': ['beta']
        }),
        'GITHUB_OUTPUT': str(out),
        'PAGES_URL': f'file://{pages}',
    }
    done = subprocess.run(['bash', str(PLAN)],
                          cwd=ws,
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    return dict(
        line.split('=', 1) for line in out.read_text().splitlines()
    ), done.stderr


def _publish(pages: Path, entries: dict) -> None:
    """Write the published hashes.json.

    Args:
        pages: Pages root.
        entries: Content of the document.
    """
    (pages / 'debs-cache').mkdir(parents=True, exist_ok=True)
    (pages / 'debs-cache' / 'hashes.json').write_text(json.dumps(entries))


def test_a_new_package_is_built_and_initialised(tmp_path: Path) -> None:
    """Nothing published yet: every package gets its first build, no failure.

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    outs, err = _plan(ws, tmp_path / 'pages')
    assert json.loads(outs['wave0']) == ['Alpha', 'gamma']
    assert json.loads(outs['wave1']) == ['beta']
    assert json.loads(outs['skipped']) == []
    assert 'never published' in err


def test_unchanged_packages_are_skipped_whatever_the_case(
    tmp_path: Path
) -> None:
    """An unchanged package leaves the matrix; names compare case-insensitively.

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    src = 'workflows/bootc-debs-builder/'
    pages = tmp_path / 'pages'
    _publish(
        pages, {
            'alpha-amd64': {
                'hash': _hash_of(ws, 'Alpha', 'echo alpha', src + 'alpha'),
                'debs': []
            },
            'beta-amd64': {
                'hash': 'stale',
                'deps_hash': 'd' * 64,
                'debs': []
            },
            'gamma-amd64': {
                'hash': _hash_of(ws, 'gamma', 'echo gamma', src + 'gamma'),
                'debs': []
            },
        }
    )
    outs, err = _plan(ws, pages)
    assert json.loads(outs['skipped']) == ['Alpha', 'gamma']
    assert json.loads(outs['wave0']) == []
    assert json.loads(outs['wave1']) == ['beta'], err


def test_a_block_scalar_build_keeps_its_trailing_newline(
    tmp_path: Path
) -> None:
    """A ``build: |`` script hashes with its final newline, as in the build job.

    The build job receives BUILD through a step output heredoc, which keeps
    that newline; a command substitution in the plan would strip it and make
    every real package look changed (the manifest always uses block scalars).

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    manifest = ws / 'workflows/bootc-debs-builder/packages.yml'
    manifest.write_text(
        'packages:\n  - name: gamma\n    sources: [gamma]\n'
        '    build: |\n      echo gamma\n      echo more\n'
    )
    src = 'workflows/bootc-debs-builder/'
    pages = tmp_path / 'pages'
    _publish(
        pages, {
            'gamma-amd64': {
                'hash':
                _hash_of(ws, 'gamma', 'echo gamma\necho more\n', src + 'gamma'),
                'debs': []
            },
        }
    )
    env_waves = {'wave0': ['gamma']}
    out = ws / 'out'
    out.write_text('')
    env = {
        **os.environ,
        'MAN': str(manifest),
        'ARCH': 'amd64',
        'WAVES': json.dumps(env_waves),
        'GITHUB_OUTPUT': str(out),
        'PAGES_URL': f'file://{pages}',
    }
    done = subprocess.run(['bash', str(PLAN)],
                          cwd=ws,
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    assert 'plan gamma/amd64: skip' in done.stderr, done.stderr


def test_a_rebuilt_dependency_rebuilds_its_dependents(tmp_path: Path) -> None:
    """beta is built when Alpha changed, even if beta's own inputs did not.

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    pages = tmp_path / 'pages'
    _publish(
        pages, {
            'alpha-amd64': {
                'hash': 'old',
                'debs': []
            },
            'beta-amd64': {
                'hash': 'x',
                'deps_hash': 'd' * 64,
                'debs': []
            },
        }
    )
    outs, err = _plan(ws, pages)
    assert 'Alpha' in json.loads(outs['wave0'])
    assert json.loads(outs['wave1']) == ['beta']
    assert 'dependency Alpha is rebuilt' in err


def test_unreadable_published_state_is_an_error(tmp_path: Path) -> None:
    """A host that answers 503 is not "nothing was built": the plan fails.

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    broken = tmp_path / 'broken'
    broken.mkdir()
    (broken / 'debs-cache').mkdir()
    (broken / 'debs-cache' / 'hashes.json').write_text('not json')
    env = {
        **os.environ,
        'MAN': str(ws / 'workflows/bootc-debs-builder/packages.yml'),
        'ARCH': 'amd64',
        'WAVES': '{"wave0": ["gamma"]}',
        'GITHUB_OUTPUT': str(ws / 'out'),
        'PAGES_URL': f'file://{broken}',
    }
    done = subprocess.run(['bash', str(PLAN)],
                          cwd=ws,
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode != 0
    assert 'not a JSON object' in done.stderr


def test_every_planned_package_gets_a_verdict(tmp_path: Path) -> None:
    """A package added later is neither lost nor failed: it is built.

    Whatever the published state, each name of WAVES ends up in exactly one
    of the filtered waves or in ``skipped``; the plan never drops one.

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    base = ws / 'workflows' / 'bootc-debs-builder'
    (base / 'delta').mkdir()
    (base / 'delta' / 'f').write_text('delta')
    with (base / 'packages.yml').open('a') as handle:
        handle.write(
            '  - name: Delta\n    sources: [delta]\n'
            '    build: echo delta\n'
        )
    pages = tmp_path / 'pages'
    src = 'workflows/bootc-debs-builder/'
    _publish(
        pages, {
            'gamma-amd64': {
                'hash': _hash_of(ws, 'gamma', 'echo gamma', src + 'gamma'),
                'debs': []
            }
        }
    )
    out = ws / 'out'
    out.write_text('')
    wanted = {'wave0': ['Alpha', 'gamma', 'Delta'], 'wave1': ['beta']}
    env = {
        **os.environ,
        'MAN': str(base / 'packages.yml'),
        'ARCH': 'amd64',
        'WAVES': json.dumps(wanted),
        'GITHUB_OUTPUT': str(out),
        'PAGES_URL': f'file://{pages}',
    }
    done = subprocess.run(['bash', str(PLAN)],
                          cwd=ws,
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    got = dict(line.split('=', 1) for line in out.read_text().splitlines())
    placed = [
        n for key in ('wave0', 'wave1', 'skipped')
        for n in json.loads(got[key])
    ]
    assert sorted(placed) == sorted(n for ns in wanted.values() for n in ns)
    assert 'Delta' in json.loads(got['wave0'])
    assert json.loads(got['skipped']) == ['gamma']


def test_a_published_build_that_is_not_served_is_rebuilt(
    tmp_path: Path
) -> None:
    """Unchanged inputs do not skip when the last-good deb is not on Pages.

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    src = 'workflows/bootc-debs-builder/'
    pages = tmp_path / 'pages'
    _publish(
        pages, {
            'gamma-amd64': {
                'hash': _hash_of(ws, 'gamma', 'echo gamma', src + 'gamma'),
                'debs': ['gamma_1_amd64.deb']
            }
        }
    )
    outs, err = _plan(ws, pages)
    assert 'gamma' in json.loads(outs['wave0'])
    assert 'gamma_1_amd64.deb is not served' in err
    (pages / 'debs-cache' / 'gamma_1_amd64.deb').write_bytes(b'x')
    outs, _ = _plan(ws, pages)
    assert 'gamma' in json.loads(outs['skipped'])


def test_fetch_published_only_for_dependencies_not_rebuilt(
    tmp_path: Path
) -> None:
    """Published debs land only for a dependency that has no artifact deb.

    Args:
        tmp_path: Scratch directory.
    """
    ws = _workspace(tmp_path)
    pages = tmp_path / 'pages'
    _publish(
        pages, {
            'alpha-amd64': {
                'hash': 'h',
                'debs': ['Alpha_1_amd64.deb']
            },
            'zeta-amd64': {
                'hash': 'h',
                'debs': ['zeta_1_amd64.deb']
            }
        }
    )
    (pages / 'debs-cache' / 'Alpha_1_amd64.deb').write_bytes(b'old')
    dest = tmp_path / 'prior'
    env = {
        **os.environ, 'MAN':
        str(ws / 'workflows/bootc-debs-builder/packages.yml'),
        'DEPS': 'Alpha',
        'ARCH': 'amd64',
        'DEST': str(dest),
        'PAGES_URL': f'file://{pages}'
    }
    fetch = ROOT / 'fetch-published' / 'fetch.sh'
    done = subprocess.run(['bash', str(fetch)],
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    assert (dest / 'Alpha_1_amd64.deb').read_bytes() == b'old'
    (dest / 'Alpha_1_amd64.deb').unlink()
    (dest / 'Alpha_2_amd64.deb').write_bytes(b'new')
    done = subprocess.run(['bash', str(fetch)],
                          env=env,
                          capture_output=True,
                          text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in dest.iterdir()) == ['Alpha_2_amd64.deb']
