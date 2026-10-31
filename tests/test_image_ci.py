"""Tests of the per-architecture image job (``scripts/image-ci``).

``podman``, ``skopeo``, ``curl``, ``trivy``, ``cosign`` and the boot test
(``image-test.sh``) are stubs first on ``PATH`` that log their arguments, so
the tests cover the orchestration (order, states, flags, exit status) and the
hash parity with the former workflow step, not the builds themselves.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / 'scripts' / 'image-ci'
WORKFLOW = ROOT / '.github' / 'workflows' / 'bootc-build.yml'
OLD_REV = '28dd042'
JQ_WORKS = shutil.which('jq') is not None and subprocess.run(
    ['jq', '-n', '1'], capture_output=True, check=False).returncode == 0
needs_jq = pytest.mark.skipif(not JQ_WORKS, reason='a working jq is required')
DIGEST = 'sha256:' + 'a' * 64
REPO = 'DaemonCores/debian-bootc'
IMAGE = 'ghcr.io/daemoncores/debian-bootc'

PODMAN = r"""#!/bin/bash
echo "podman $*" >>"${STUB_LOG}"
case "$1" in
  build)
    args=" $* "
    if [ -n "${FAIL_BUILD:-}" ] && [[ "${args}" == *"${FAIL_BUILD}"* ]]; then
      echo "build error"
      exit 1
    fi
    if [ -n "${STALE_MARKER:-}" ] && [[ "${args}" == *"--cache-to"* ]] \
      && [ ! -e "${STALE_MARKER}" ]; then
      : >"${STALE_MARKER}"
      echo "locating image with ID abc"
      exit 1
    fi
    ;;
  run)
    if [ -n "${FAIL_RUN:-}" ] && [[ " $* " == *"${FAIL_RUN}"* ]]; then
      exit 1
    fi
    ;;
  push)
    if [ -n "${FAIL_PUSH:-}" ] && [[ " $* " == *"${FAIL_PUSH}"* ]]; then
      exit 1
    fi
    if [ "$2" = "--digestfile" ]; then
      echo "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" >"$3"
    fi
    ;;
  save)
    while [ "$#" -gt 0 ]; do
      [ "$1" = "-o" ] && : >"$2"
      shift
    done
    ;;
esac
exit 0
"""

SKOPEO = r"""#!/bin/bash
echo "skopeo $*" >>"${STUB_LOG}"
case " $* " in
  *"{{.Digest}}"*) echo "sha256:base" ;;
  *)
    ref="${@: -1}"
    ref="${ref#docker://}"
    file="${PUBLISHED}/${ref//[\/:]/_}"
    [ -f "${file}" ] || exit 1
    cat "${file}"
    ;;
esac
"""

CURL = """#!/bin/bash
echo '{"mesa-amd64":"1","linux-image-amd64":"2","mesa-arm64":"3"}'
"""

TRIVY = """#!/bin/bash
echo "trivy $*" >>"${STUB_LOG}"
[ -z "${FAIL_TRIVY:-}" ]
"""

COSIGN = """#!/bin/bash
echo "cosign $*" >>"${STUB_LOG}"
"""

IMAGE_TEST = r"""#!/bin/bash
echo "image-test $* ARCH=${ARCH} STATE=${IMAGE_TEST_STATE:-}" >>"${STUB_LOG}"
serial="${IMAGE_TEST_STATE}/logs/${2//[^A-Za-z0-9._-]/_}.serial.log"
if [ "$1" = "test" ]; then
  if [ -n "${FAIL_TEST:-}" ] && [[ "$2" == *"${FAIL_TEST}"* ]]; then
    mkdir -p "${IMAGE_TEST_STATE}/logs"
    echo "boot failed" >"${serial}"
    exit 7
  fi
  if [ -n "${HANG_TEST:-}" ] && [[ "$2" == *"${HANG_TEST}"* ]]; then
    exec sleep 30
  fi
fi
[ "$1" != "prepare" ] || [ -z "${FAIL_PREPARE:-}" ]
"""

SUDO = """#!/bin/bash
exec "$@"
"""

BASE_CF = 'FROM debian:trixie\nARG ARCH=amd64\n'
MAIN_CF = ('ARG BASE_IMAGE=localhost/debian-bootc-base\n'
           'FROM ${BASE_IMAGE}\nARG ARCH=amd64\n')
ISO_CF = ('ARG ARCH=amd64\n'
          'ARG BASE_IMAGE=ghcr.io/daemoncores/debian-bootc:latest_${ARCH}\n'
          'FROM ${BASE_IMAGE}\n')


def _script(path: Path, body: str) -> None:
    """Write an executable script.

    Args:
        path: Destination.
        body: Script text.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Sandbox:
    """A scratch repository with stubs first on ``PATH``."""

    def __init__(self, tmp: Path) -> None:
        """Lay out the repository, the stubs and the state directories.

        Args:
            tmp: Scratch directory.
        """
        self.root = tmp / 'repo'
        self.root.mkdir()
        self.log = tmp / 'stub.log'
        self.log.write_text('', encoding='utf-8')
        self.summary = tmp / 'summary.md'
        self.runner_temp = tmp / 'runner'
        self.published = tmp / 'published'
        self.published.mkdir()
        bindir = tmp / 'bin'
        for name, body in (('podman', PODMAN), ('skopeo', SKOPEO),
                           ('curl', CURL), ('trivy', TRIVY),
                           ('cosign', COSIGN), ('sudo', SUDO)):
            _script(bindir / name, body)
        self.image_test = tmp / 'image-test.sh'
        _script(self.image_test, IMAGE_TEST)
        (self.root / 'Containerfile.base').write_text(BASE_CF)
        (self.root / 'Containerfile').write_text(MAIN_CF)
        (self.root / 'Containerfile.iso').write_text(ISO_CF)
        (self.root / 'workflows').mkdir()
        (self.root / 'workflows' / 'tests.yml').write_text('tests: []\n')
        git = ['git', '-c', 'user.email=t@t', '-c', 'user.name=t']
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        subprocess.run(['git', 'add', '-A'], cwd=self.root, check=True)
        subprocess.run(git + ['commit', '-q', '-m', 'init'],
                       cwd=self.root,
                       check=True)
        self.env = dict(os.environ,
                        PATH=f'{bindir}:{os.environ["PATH"]}',
                        SUDO='',
                        STUB_LOG=str(self.log),
                        PUBLISHED=str(self.published),
                        RUNNER_TEMP=str(self.runner_temp),
                        GITHUB_STEP_SUMMARY=str(self.summary),
                        GITHUB_REPOSITORY=REPO,
                        GITHUB_EVENT_NAME='push',
                        GITHUB_REF='refs/heads/feature',
                        GLOBAL_HASH='g' * 64,
                        IMAGE_TEST=str(self.image_test),
                        COSIGN_PUBLIC_KEY='pub')
        self.env.pop('FORCE_NO_CACHE', None)

    def run(self,
            entries: list[dict[str, str]],
            *phases: str,
            arch: str = 'amd64',
            **env: str) -> subprocess.CompletedProcess[str]:
        """Run build-arch.sh in the repository.

        Args:
            entries: Matrix entries of the architecture.
            phases: Phases to run (all when empty).
            arch: Architecture.
            env: Extra environment variables.

        Returns:
            The completed process.
        """
        import json
        full = dict(self.env, ARCH=arch, ENTRIES=json.dumps(entries), **env)
        return subprocess.run(['bash', str(CI / 'build-arch.sh'), *phases],
                              cwd=self.root,
                              env=full,
                              capture_output=True,
                              text=True,
                              check=False)

    def calls(self, prefix: str = '') -> list[str]:
        """Return the logged stub calls starting with a prefix.

        Args:
            prefix: Required start of the call line.

        Returns:
            The matching lines, in call order.
        """
        return [
            ln for ln in self.log.read_text().splitlines()
            if ln.startswith(prefix)
        ]

    def states(self) -> dict[str, str]:
        """Return the final state of every image.

        Returns:
            Mapping image tag to state.
        """
        tsv = self.runner_temp / 'image-ci' / 'state.tsv'
        rows = [ln.split('\t') for ln in tsv.read_text().splitlines()]
        return {r[0]: r[7] for r in rows}

    def row(self, tag: str) -> list[str]:
        """Return the TSV columns of one image.

        Args:
            tag: Image tag.

        Returns:
            The columns.
        """
        tsv = self.runner_temp / 'image-ci' / 'state.tsv'
        for ln in tsv.read_text().splitlines():
            cols = ln.split('\t')
            if cols[0] == tag:
                return cols
        raise KeyError(tag)

    def publish(self, tag: str, arch: str, label: str) -> None:
        """Record the content-hash label of a published image.

        Args:
            tag: Image tag.
            arch: Architecture.
            label: Content hash label.
        """
        key = f'{IMAGE}:{tag}_{arch}'.replace('/', '_').replace(':', '_')
        (self.published / key).write_text(label + '\n')


def entry(tag: str,
          file: str = 'Containerfile',
          suffix: str = '',
          twin: str = '') -> dict[str, str]:
    """Build one matrix entry.

    Args:
        tag: Image tag.
        file: Containerfile name.
        suffix: Kernel suffix.
        twin: Twin kernel suffix.

    Returns:
        The entry.
    """
    return {
        'file': file,
        'tag': tag,
        'arch': 'amd64',
        'kernel_suffix': suffix,
        'twin_suffix': twin
    }


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    """Provide a sandbox.

    Args:
        tmp_path: Scratch directory.

    Returns:
        The sandbox.
    """
    return Sandbox(tmp_path)


LATEST = [entry('latest', twin='-nosign'), entry('iso', 'Containerfile.iso')]


# ----------------------------------------------------------------- hash ----
def _old_chash_script() -> str:
    """Return the run script of the former "Compute content hash" step.

    Returns:
        The shell text, GitHub expressions replaced by their values.
    """
    text = subprocess.run(
        ['git', 'show', f'{OLD_REV}:.github/workflows/bootc-build.yml'],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True).stdout
    workflow = cast(dict[str, Any], yaml.safe_load(text))
    steps = workflow['jobs']['build-test-push']['steps']
    run = next(s['run'] for s in steps if s.get('id') == 'chash')
    values = {'github.event_name': 'push', 'inputs.force_no_cache': 'false'}
    return re.sub(r'\$\{\{\s*([\w.]+)\s*\}\}',
                  lambda m: values[m.group(1)], run)


@needs_jq
def test_hash_is_identical_to_the_former_workflow_step(sb: Sandbox) -> None:
    """The moved formula yields the hashes the workflow step produced."""
    out = sb.root.parent / 'old.out'
    env = dict(sb.env,
               SUDO='sudo',
               CF_FILE='Containerfile',
               CF_TAG='latest',
               ARCH='amd64',
               PRIMARY='latest_amd64',
               IMAGE=IMAGE,
               BOOTC_GPG_SHA256='key',
               GITHUB_OUTPUT=str(out))
    subprocess.run(['bash', '-c', _old_chash_script()],
                   cwd=sb.root,
                   env=env,
                   check=True,
                   capture_output=True)
    old = dict(
        ln.split('=', 1) for ln in out.read_text().splitlines() if '=' in ln)
    script = (f'. {CI}/hash.sh; ci_hash_inputs; h=$(ci_image_hash '
              'Containerfile latest); echo "hash=$h"; '
              'echo "twin_hash=$(ci_twin_hash "$h")"; '
              'echo "debs_hash=${DEBS_HASH}"; '
              'echo "kernel_debs_hash=${KERNEL_DEBS_HASH}"')
    done = subprocess.run(['bash', '-c', script],
                          cwd=sb.root,
                          env=dict(env, SUDO='sudo'),
                          check=True,
                          capture_output=True,
                          text=True)
    new = dict(ln.split('=', 1) for ln in done.stdout.splitlines())
    for key in ('hash', 'twin_hash', 'debs_hash', 'kernel_debs_hash'):
        assert len(old[key]) == 64
        assert new[key] == old[key], key


@pytest.mark.parametrize(
    ('prior', 'event', 'force', 'expected'),
    [
        ('abc', 'push', 'false', 'skip'),
        ('abc', 'schedule', 'false', 'build'),
        ('abc', 'push', 'true', 'build'),
        ('other', 'push', 'false', 'build'),
        ('', 'push', 'false', 'build'),
    ],
)
def test_skip_decision(prior: str, event: str, force: str,
                       expected: str) -> None:
    """A matching published hash skips, except on a refresh run.

    Args:
        prior: Published label.
        event: Event name.
        force: force_no_cache input.
        expected: Decision.
    """
    env = dict(os.environ, GITHUB_EVENT_NAME=event, FORCE_NO_CACHE=force)
    done = subprocess.run(
        ['bash', '-c', f'. {CI}/hash.sh; ci_skip_decision "{prior}" abc'],
        env=env,
        check=True,
        capture_output=True,
        text=True)
    assert done.stdout.strip() == expected


@needs_jq
def test_hash_phase_skips_unchanged_images(sb: Sandbox) -> None:
    """An image whose published label matches is skipped-unchanged."""
    sb.run(LATEST, 'hash')
    latest, twin = sb.row('latest')[6], sb.row('latest-nosign')[6]
    sb.publish('latest', 'amd64', latest)
    sb.publish('latest-nosign', 'amd64', 'stale')
    done = sb.run(LATEST, 'hash')
    assert done.returncode == 0, done.stderr
    states = sb.states()
    assert states['latest'] == 'skipped-unchanged'
    assert states['latest-nosign'] == 'pending'
    assert latest != twin


# -------------------------------------------------------------- prepare ----
@pytest.mark.parametrize(('arch', 'expected'), [
    ('amd64', {'qemu-system-x86', 'ovmf'}),
    ('arm64', {'qemu-system-arm', 'qemu-efi-aarch64'}),
])
def test_prepare_lists_the_packages_of_the_architecture(
        arch: str, expected: set[str]) -> None:
    """One package list per architecture, with the common tools.

    Args:
        arch: Architecture.
        expected: Architecture specific packages.
    """
    done = subprocess.run(['bash', str(CI / 'prepare.sh'), '--list'],
                          env=dict(os.environ, ARCH=arch),
                          check=True,
                          capture_output=True,
                          text=True)
    pkgs = set(done.stdout.split())
    assert {'skopeo', 'jq', 'python3-venv', 'openssl'} <= pkgs
    assert expected <= pkgs
    assert not ({'qemu-system-x86', 'qemu-system-arm'} - expected) & pkgs


def test_prepare_installs_in_one_apt_call(sb: Sandbox) -> None:
    """A single apt-get install receives every package."""
    _script(sb.root.parent / 'bin' / 'apt-get',
            '#!/bin/bash\necho "apt-get $*" >>"${STUB_LOG}"\n')
    done = subprocess.run(['bash', str(CI / 'prepare.sh')],
                          env=dict(sb.env, ARCH='arm64'),
                          check=False,
                          capture_output=True,
                          text=True)
    assert done.returncode == 0, done.stderr
    installs = sb.calls('apt-get install')
    assert len(installs) == 1
    assert 'qemu-efi-aarch64' in installs[0]
    assert 'skopeo' in installs[0]


def test_prepare_rejects_an_unknown_architecture() -> None:
    """An architecture without a firmware package fails loudly."""
    done = subprocess.run(['bash', str(CI / 'prepare.sh'), '--list'],
                          env=dict(os.environ, ARCH='mips'),
                          check=False,
                          capture_output=True,
                          text=True)
    assert done.returncode != 0


# ---------------------------------------------------------------- build ----
@needs_jq
def test_parent_of_a_derived_image_is_read_from_its_from_line(
        sb: Sandbox) -> None:
    """FROM naming an image of this repository records its parent tag."""
    sb.run(LATEST, 'hash')
    assert sb.row('iso')[5] == 'latest'
    assert sb.row('latest')[5] == '-'


@needs_jq
def test_build_order_and_no_cache_flags(sb: Sandbox) -> None:
    """Base once; per Containerfile primary then twin; no-cache on primaries."""
    entries = [
        entry('latest', suffix='-16k', twin='-16k-nosign'),
        entry('latest-4k'),
        entry('other', 'Containerfile.other'),
    ]
    (sb.root / 'Containerfile.other').write_text(MAIN_CF)
    done = sb.run(entries,
                  'hash',
                  'build',
                  arch='arm64',
                  FORCE_NO_CACHE='true')
    assert done.returncode == 0, done.stderr
    builds = sb.calls('podman build')
    assert len(builds) == 5
    tags = [re.search(r'--tag (\S+)', b).group(1)  # type: ignore[union-attr]
            for b in builds]
    assert tags[0] == 'localhost/debian-bootc-base'
    assert [t for t in tags if t.endswith('_arm64')] == [
        f'{IMAGE}:latest_arm64', f'{IMAGE}:latest-nosign_arm64',
        f'{IMAGE}:latest-4k_arm64', f'{IMAGE}:other_arm64'
    ]
    nocache = [('--no-cache' in b) for b in builds]
    # base, latest, (twin: no), (4k: no), other
    assert nocache == [True, True, False, False, True]
    assert 'KERNEL_SUFFIX=-16k-nosign' in builds[2]
    assert 'ARM_DEVICES=rpi5 rk3588' in builds[1]
    assert 'ARM_DEVICES=rpi3 rpi4' in builds[3]
    assert '--tag ' + f'{IMAGE}:latest ' not in ' '.join(builds)


@needs_jq
def test_cache_to_only_on_main_and_canonical_tags_only_on_amd64(
        sb: Sandbox) -> None:
    """The layer cache is written on main; extra tags follow the arch."""
    sb.run([entry('latest')], 'hash', 'build')
    assert not [b for b in sb.calls('podman build') if '--cache-to' in b]
    amd = sb.calls('podman build')[1]
    assert f'--tag {IMAGE}:latest ' in amd + ' '
    sb.log.write_text('')
    sb.run([entry('latest')],
           'hash',
           'build',
           arch='arm64',
           GITHUB_REF='refs/heads/main')
    builds = sb.calls('podman build')
    assert all('--cache-to' in b for b in builds)
    assert f'--tag {IMAGE}:latest ' not in builds[1] + ' '


@needs_jq
def test_stale_cache_blob_retries_without_cache_to(sb: Sandbox) -> None:
    """A failed cache push is retried read-only and the build succeeds."""
    done = sb.run([entry('latest')],
                  'hash',
                  'build',
                  GITHUB_REF='refs/heads/main',
                  STALE_MARKER=str(sb.root.parent / 'stale'))
    assert done.returncode == 0, done.stderr
    assert sb.states()['latest'] == 'built'
    builds = sb.calls('podman build')
    assert '--cache-to' in builds[0]
    assert '--cache-to' not in builds[1]
    assert 'warning::buildah cache push failed' in done.stdout


@needs_jq
def test_a_failed_build_is_recorded_and_the_loop_continues(
        sb: Sandbox) -> None:
    """The twin fails; the primary still goes through; exit status is 1."""
    entries = [entry('latest', twin='-nosign'), entry('second')]
    done = sb.run(entries, 'hash', 'build', FAIL_BUILD='latest-nosign_amd64')
    assert done.returncode == 1
    assert sb.states() == {
        'latest': 'built',
        'latest-nosign': 'build-failed',
        'second': 'built'
    }
    assert '| `' + f'{IMAGE}:latest-nosign_amd64' + '` | build-failed' \
        in sb.summary.read_text()


@needs_jq
def test_a_failed_base_fails_every_image_of_the_wave(sb: Sandbox) -> None:
    """Nothing is built on a base that did not build."""
    done = sb.run(LATEST,
                  'hash',
                  'build',
                  'wave2',
                  FAIL_BUILD='localhost/debian-bootc-base')
    assert done.returncode == 1
    assert sb.states() == {
        'latest': 'build-failed',
        'latest-nosign': 'build-failed',
        'iso': 'blocked'
    }
    assert len(sb.calls('podman build')) == 1


# ---------------------------------------------------------------- waves ----
@needs_jq
def test_the_derived_image_is_built_after_the_tests_on_the_local_parent(
        sb: Sandbox) -> None:
    """Wave 2 follows the boot test of its parent and builds on it."""
    done = sb.run(LATEST)
    assert done.returncode == 0, done.stderr
    log = sb.log.read_text()
    assert log.index('image-test test') < log.rindex('podman build')
    iso = [b for b in sb.calls('podman build') if 'Containerfile.iso' in b]
    assert len(iso) == 1
    assert f'--build-arg BASE_IMAGE={IMAGE}:latest_amd64' in iso[0]
    assert '--pull=missing' in iso[0]
    assert sb.states()['iso'] == 'ok'
    # The installer image has no SSH server: no boot test, lint only.
    assert not [t for t in sb.calls('image-test test') if '/iso' in t
                or ':iso_' in t]
    assert any(f'{IMAGE}:iso_amd64 bootc container lint' in c
               for c in sb.calls('podman run'))


@needs_jq
def test_the_derived_image_is_blocked_when_its_parent_failed(
        sb: Sandbox) -> None:
    """A parent that did not pass blocks the image built on it."""
    done = sb.run(LATEST, FAIL_TEST='latest_amd64')
    assert done.returncode == 1
    states = sb.states()
    assert states['latest'] == 'test-failed'
    assert states['iso'] == 'blocked'
    assert not [b for b in sb.calls('podman build') if 'Containerfile.iso' in b]


@needs_jq
def test_a_skipped_parent_lets_the_derived_image_build_on_the_published_one(
        sb: Sandbox) -> None:
    """With an unchanged parent the derived image is built, pulling it."""
    sb.run(LATEST, 'hash')
    sb.publish('latest', 'amd64', sb.row('latest')[6])
    sb.publish('latest-nosign', 'amd64', sb.row('latest-nosign')[6])
    sb.log.write_text('')
    done = sb.run(LATEST)
    assert done.returncode == 0, done.stderr
    states = sb.states()
    assert states['latest'] == 'skipped-unchanged'
    assert states['iso'] == 'ok'
    assert len(sb.calls('podman build')) == 1
    assert not sb.calls('image-test test')


@needs_jq
def test_an_unchanged_derived_image_follows_a_rebuilt_parent(
        sb: Sandbox) -> None:
    """The derived image is rebuilt when its parent was, even if unchanged."""
    sb.run(LATEST, 'hash')
    sb.publish('iso', 'amd64', sb.row('iso')[6])
    done = sb.run(LATEST)
    assert done.returncode == 0, done.stderr
    assert sb.states()['iso'] == 'ok'
    assert [b for b in sb.calls('podman build') if 'Containerfile.iso' in b]
    sb.log.write_text('')
    sb.publish('latest', 'amd64', sb.row('latest')[6])
    sb.publish('latest-nosign', 'amd64', sb.row('latest-nosign')[6])
    sb.run(LATEST)
    assert sb.states()['iso'] == 'skipped-unchanged'
    assert not sb.calls('podman build')


@needs_jq
def test_an_unknown_parent_blocks(sb: Sandbox) -> None:
    """A parent that is not part of this run cannot be vouched for."""
    done = sb.run([entry('iso', 'Containerfile.iso')])
    assert done.returncode == 1
    assert sb.states() == {'iso': 'blocked'}


# ----------------------------------------------------- lint, test, scan ----
@needs_jq
def test_lint_failure_is_recorded(sb: Sandbox) -> None:
    """A failing lint stops the image before its boot test."""
    done = sb.run([entry('latest')], FAIL_RUN='bootc container lint')
    assert done.returncode == 1
    assert sb.states() == {'latest': 'lint-failed'}
    assert not sb.calls('image-test test')


@needs_jq
def test_boot_tests_use_one_prepare_and_one_child_process_per_image(
        sb: Sandbox) -> None:
    """Prepare once, test each image with its RAM budget, teardown once."""
    entries = [entry('latest', twin='-nosign'), entry('minimal')]
    (sb.root / 'Containerfile.minimal').write_text(MAIN_CF)
    entries[1]['file'] = 'Containerfile.minimal'
    done = sb.run(entries)
    assert done.returncode == 0, done.stderr
    calls = sb.calls('image-test')
    assert [c.split()[1] for c in calls] == [
        'prepare', 'test', 'test', 'test', 'teardown'
    ]
    tests = [c.split()[1:4] for c in calls if c.startswith('image-test test')]
    assert tests == [
        ['test', f'{IMAGE}:latest_amd64', '0'],
        ['test', f'{IMAGE}:latest-nosign_amd64', '0'],
        ['test', f'{IMAGE}:minimal_amd64', '15'],
    ]
    assert 'STATE=' + str(sb.runner_temp / 'image-ci' / 'image-test') \
        in calls[1]


@needs_jq
def test_a_failed_boot_test_is_recorded_with_its_serial_log(
        sb: Sandbox) -> None:
    """The failure is reported with the serial log; the next image runs."""
    entries = [entry('latest', twin='-nosign')]
    done = sb.run(entries, FAIL_TEST='latest_amd64')
    assert done.returncode == 1
    assert sb.states() == {'latest': 'test-failed', 'latest-nosign': 'ok'}
    text = sb.summary.read_text()
    assert 'boot failed' in text
    assert (sb.runner_temp / 'image-ci' / 'failed-logs' /
            'serial-latest.log').exists()


@needs_jq
def test_a_hung_boot_test_is_killed_by_the_timeout(sb: Sandbox) -> None:
    """The child process is bounded by BOOT_TEST_TIMEOUT."""
    done = sb.run([entry('latest')], HANG_TEST='latest_amd64',
                  BOOT_TEST_TIMEOUT='1')
    assert done.returncode == 1
    assert sb.row('latest')[7] == 'test-failed'
    assert 'timeout' in sb.row('latest')[8]


@needs_jq
def test_a_failed_prepare_fails_the_tests_not_the_loop(sb: Sandbox) -> None:
    """Every image to test is reported, nothing is pushed or hidden."""
    done = sb.run(LATEST, FAIL_PREPARE='1')
    assert done.returncode == 1
    assert sb.states()['latest'] == 'test-failed'
    assert sb.states()['iso'] == 'blocked'
    assert len(sb.calls('image-test prepare')) == 1
    assert not sb.calls('image-test teardown')


@needs_jq
def test_scan_is_report_only_and_removes_its_archive(sb: Sandbox) -> None:
    """A scanner failure is a warning; the archive never stays."""
    done = sb.run([entry('latest', twin='-nosign')], FAIL_TRIVY='1')
    assert done.returncode == 0, done.stderr
    assert len(sb.calls('trivy image --input')) == 1
    assert 'warning::vulnerability scan of latest failed' in done.stdout
    assert not (sb.runner_temp / 'image-ci' / 'scan-image.tar').exists()


# ----------------------------------------------------------- push, sign ----
@needs_jq
def test_nothing_is_pushed_outside_main(sb: Sandbox) -> None:
    """A branch run builds and tests but never pushes or signs."""
    done = sb.run(LATEST)
    assert done.returncode == 0, done.stderr
    assert not sb.calls('podman push')
    assert not sb.calls('cosign')


@needs_jq
def test_main_pushes_ok_images_and_signs_once(sb: Sandbox) -> None:
    """Per-arch and canonical tags are pushed, one sign call covers all."""
    done = sb.run(LATEST,
                  FAIL_TEST='latest-nosign_amd64',
                  GITHUB_REF='refs/heads/main',
                  COSIGN_PRIVATE_KEY='k')
    assert done.returncode == 1
    states = sb.states()
    assert states['latest'] == 'pushed'
    assert states['latest-nosign'] == 'test-failed'
    assert states['iso'] == 'pushed'
    pushes = sb.calls('podman push')
    sha = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                         cwd=sb.root,
                         check=True,
                         capture_output=True,
                         text=True).stdout.strip()
    assert any(p.endswith(f'{IMAGE}:latest_amd64') for p in pushes)
    assert f'podman push {IMAGE}:{sha}' in pushes
    assert f'podman push {IMAGE}:latest' in pushes
    assert not [p for p in pushes if 'nosign' in p]
    signs = sb.calls('cosign sign')
    assert len(signs) == len([p for p in pushes if '--digestfile' in p])
    assert all('@' + DIGEST in s for s in signs)


@needs_jq
def test_a_failed_push_is_recorded_and_not_signed(sb: Sandbox) -> None:
    """The failing image has no signature; the other ones go on."""
    entries = [entry('latest'), entry('second')]
    done = sb.run(entries,
                  GITHUB_REF='refs/heads/main',
                  FAIL_PUSH='latest_amd64')
    assert done.returncode == 1
    states = sb.states()
    assert states['latest'] == 'push-failed'
    assert states['second'] == 'pushed'
    assert len(sb.calls('cosign sign')) == 1


@needs_jq
def test_a_failed_signature_fails_the_job(sb: Sandbox) -> None:
    """A signing error is reported and the exit status is 1."""
    _script(sb.root.parent / 'bin' / 'cosign', '#!/bin/bash\nexit 1\n')
    done = sb.run([entry('latest')], GITHUB_REF='refs/heads/main')
    assert done.returncode == 1
    assert 'Signature: **failed**' in sb.summary.read_text()


@needs_jq
def test_unchanged_images_are_reported_not_failed(sb: Sandbox) -> None:
    """A fully unchanged arch builds nothing and succeeds."""
    entries = [entry('latest')]
    sb.run(entries, 'hash')
    sb.publish('latest', 'amd64', sb.row('latest')[6])
    sb.log.write_text('')
    done = sb.run(entries)
    assert done.returncode == 0, done.stderr
    assert not sb.calls('podman')
    assert 'skipped-unchanged' in sb.summary.read_text()


# ------------------------------------------------------------- workflow ----
def _workflow() -> dict[str, Any]:
    """Load bootc-build.yml.

    Returns:
        The parsed workflow.
    """
    return cast(dict[str, Any], yaml.safe_load(WORKFLOW.read_text()))


def test_workflow_runs_one_job_per_arch_through_the_script() -> None:
    """build-test-push is the per-arch job; build-iso is gone."""
    jobs = _workflow()['jobs']
    assert 'build-iso' not in jobs
    job = jobs['build-test-push']
    assert job['strategy']['matrix']['group'] == \
        '${{ fromJSON(needs.discover.outputs.arch_matrix) }}'
    assert job['runs-on'] == '${{ matrix.group.runner }}'
    runs = ' '.join(s.get('run', '') for s in job['steps'])
    assert 'scripts/image-ci/build-arch.sh' in runs
    assert 'scripts/image-ci/prepare.sh' in runs
    assert set(jobs['manifest']['needs']) == {'discover', 'build-test-push'}


def test_workflow_keeps_every_input() -> None:
    """The reusable workflow inputs are unchanged."""
    # YAML 1.1 reads the key `on` as the boolean True.
    triggers = cast(dict[Any, Any], _workflow())[True]
    inputs = triggers['workflow_call']['inputs']
    assert set(inputs) == {
        'force_no_cache', 'bootc_gpg_sha256', 'apt_url', 'nosign_images'
    }

