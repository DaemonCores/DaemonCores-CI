"""Tests of the unsigned-kernel twin of the ``both`` signing mode.

The discover step of ``bootc-build.yml`` is run on a scratch repository of
Containerfiles, and the manifest filter is run on a small manifest; the wiring
of the other files is checked on the text, since a workflow cannot be run
outside GitHub.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest
from typed_docs import (
    as_list,
    as_mapping,
    as_mappings,
    as_str,
    as_str_mapping,
    load_json,
    load_yaml,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'
JQ_WORKS = shutil.which('jq') is not None and subprocess.run(['jq', '-n', '1'],
                                                             capture_output=True
                                                             ).returncode == 0
needs_jq = pytest.mark.skipif(not JQ_WORKS, reason='a working jq is required')


class Entry(TypedDict):
    """One image of the build matrix.

    Attributes:
        file: Containerfile the image is built from.
        tag: Image tag.
        arch: Target architecture.
        kernel_suffix: Suffix of the kernel variant, empty for the default.
        twin_suffix: Suffix of the usage twin, always empty since L1.
    """

    file: str
    tag: str
    arch: str
    kernel_suffix: str
    twin_suffix: str


class Group(TypedDict):
    """The matrix entries built by one runner.

    Attributes:
        arch: Architecture every entry of the group targets.
        runner: GitHub runner the group is built on.
        entries: Images of the group, in build order.
    """

    arch: str
    runner: str
    entries: list[Entry]


class Discovered(TypedDict):
    """The matrices written by the discover step.

    Attributes:
        groups: Entries grouped by architecture.
        wave1: Every entry, flattened.
        manifest: Raw entries of the manifest matrix.
    """

    groups: list[Group]
    wave1: list[Entry]
    manifest: list[dict[str, object]]


def _entry(value: object) -> Entry:
    """Narrow one matrix entry.

    Args:
        value: A parsed matrix entry.

    Returns:
        The entry, fields typed.
    """
    data = as_str_mapping(value)
    return Entry(
        file=as_str(data['file']),
        tag=as_str(data['tag']),
        arch=as_str(data['arch']),
        kernel_suffix=as_str(data['kernel_suffix']),
        twin_suffix=as_str(data['twin_suffix']),
    )


def _group(value: object) -> Group:
    """Narrow one architecture group.

    Args:
        value: A parsed architecture group.

    Returns:
        The group, entries typed.
    """
    data = as_str_mapping(value)
    return Group(
        arch=as_str(data['arch']),
        runner=as_str(data['runner']),
        entries=[_entry(entry) for entry in as_list(data['entries'])],
    )


def _discover_script() -> str:
    """Return the shell of the discover step of ``bootc-build.yml``.

    Returns:
        The ``run`` script of the step that builds the matrices.
    """
    workflow = as_mapping(
        load_yaml((WORKFLOWS / 'bootc-build.yml').read_text())
    )
    discover = as_str_mapping(as_mapping(workflow['jobs'])['discover'])
    steps = as_list(discover['steps'])
    return next(
        as_str(step['run']) for step in as_mappings(steps)
        if step.get('id') == 'list'
    )


def _discover(
    tmp_path: Path,
    nosign: bool = False,
    extra: dict[str, str] | None = None
) -> Discovered:
    """Run the discover step on a repository with three Containerfiles.

    Args:
        tmp_path: Scratch repository root.
        nosign: Accepted for compatibility; no twin exists since L1.
        extra: Additional Containerfiles (name to content).

    Returns:
        The images of every architecture (flattened) and the manifest matrix.
    """
    generic = (
        'LABEL org.daemoncores.arm64-4k-image="true"\n'
        'LABEL org.daemoncores.unsigned-kernel-image="true"\n'
    )
    (tmp_path / 'Containerfile').write_text('FROM debian:trixie\n' + generic)
    (tmp_path / 'Containerfile.board').write_text('FROM debian:trixie\n')
    (tmp_path / 'Containerfile.iso'
     ).write_text('FROM ghcr.io/daemoncores/debian-bootc:latest_amd64\n')
    for name, content in (extra or {}).items():
        (tmp_path / name).write_text(content)
    out = tmp_path / 'out'
    env = dict(
        os.environ,
        GITHUB_OUTPUT=str(out),
        GITHUB_REPOSITORY='DaemonCores/debian-bootc',
        NOSIGN_IMAGES='true' if nosign else 'false'
    )
    subprocess.run(['bash', '-c', _discover_script()],
                   cwd=tmp_path,
                   env=env,
                   check=True,
                   capture_output=True)
    lines: dict[str, str] = {}
    for line in out.read_text().splitlines():
        if '=' in line:
            key, _, value = line.partition('=')
            lines[key] = value
    parsed = as_list(load_json(lines['arch_matrix']))
    groups = [_group(group) for group in parsed]
    manifest = [
        as_str_mapping(entry)
        for entry in as_list(load_json(lines['manifest_matrix']))
    ]
    return Discovered(
        groups=groups,
        wave1=[entry for group in groups for entry in group['entries']],
        manifest=manifest,
    )


@needs_jq
def test_there_is_one_image_per_tag_and_arch(tmp_path: Path) -> None:
    """No lock or autoupdate dimension is left in the matrices.

    Args:
        tmp_path: Scratch repository root.
    """
    result = _discover(tmp_path, nosign=False)
    keys = {key for entry in result['wave1'] for key in entry}
    # No GPU dimension: one image carries every vendor.
    assert keys == {'file', 'tag', 'arch', 'kernel_suffix', 'twin_suffix'}
    assert all('lock' not in entry for entry in result['manifest'])
    pairs = [(e['tag'], e['arch']) for e in result['wave1']]
    assert len(pairs) == len(set(pairs))


@needs_jq
def test_images_are_grouped_by_architecture_in_build_order(
    tmp_path: Path
) -> None:
    """One group per arch on its native runner, the -4k image after its parent.

    Args:
        tmp_path: Scratch repository root.
    """
    groups = _discover(tmp_path, nosign=False)['groups']
    assert [(g['arch'], g['runner'])
            for g in groups] == [('amd64', 'ubuntu-latest'),
                                 ('arm64', 'ubuntu-24.04-arm')]
    amd64, arm64 = ([e['tag'] for e in g['entries']] for g in groups)
    assert amd64 == ['latest', 'board', 'iso']
    assert arm64 == ['latest', 'latest-4k', 'board', 'iso']
    assert all(e['arch'] == g['arch'] for g in groups for e in g['entries'])


@needs_jq
def test_twin_only_for_the_generic_images(tmp_path: Path) -> None:
    """One image per variant: NO twin, and the legacy label is ignored.

    The twin is not a matrix entry: even a Containerfile carrying the
    ``org.daemoncores.unsigned-kernel-image`` LABEL of the former ``both``
    mode gets one image per tag, on the enforced-at-install kernel.

    Args:
        tmp_path: Scratch repository root.
    """
    result = _discover(tmp_path, nosign=True)
    assert not [e for e in result['wave1'] if e['tag'].endswith('-nosign')]
    twin_suffix = {(e['tag'], e['arch']): e['twin_suffix']
                   for e in result['wave1']}
    assert twin_suffix == {
        ('latest', 'amd64'): '',
        ('latest', 'arm64'): '',
        ('latest-4k', 'arm64'): '',
        ('board', 'amd64'): '',
        ('board', 'arm64'): '',
        ('iso', 'amd64'): '',
        ('iso', 'arm64'): '',
    }
    manifest = {as_str(e['tag']): e['arches'] for e in result['manifest']}
    assert manifest['latest'] == ['amd64', 'arm64']
    assert manifest['latest-4k'] == ['arm64']
    assert manifest['board'] == ['amd64', 'arm64']
    plain = {
        e['kernel_suffix']
        for e in result['wave1'] if e['tag'] == 'latest-4k'
    }
    assert plain == {''}


@needs_jq
def test_no_twin_without_the_input(tmp_path: Path) -> None:
    """Without ``nosign_images`` — actually under any mode — no twin is planned.

    Args:
        tmp_path: Scratch repository root.
    """
    result = _discover(tmp_path, nosign=False)
    assert not [e for e in result['wave1'] if e['twin_suffix']]
    assert not [e for e in result['manifest'] if 'nosign' in as_str(e['tag'])]
    assert not [e for e in result['wave1'] if 'nosign' in e['tag']]


def _expand(unsigned: str) -> list[str]:
    """Run ``expand.jq`` on a manifest holding one unsigned-kernel twin.

    Args:
        unsigned: Value of ``UNSIGNED_KERNEL``.

    Returns:
        The kept package names.
    """
    manifest = {
        'packages': [{
            'name': 'kernel'
        }, {
            'name': 'kernel-nosign',
            'when': {
                'arch': 'amd64',
                'unsigned-kernel': True
            }
        }]
    }
    result = subprocess.run([
        'jq', '-c', '-f',
        str(ROOT / 'manifest-expand' / 'expand.jq')
    ],
                            input=json.dumps(manifest),
                            env=dict(os.environ, UNSIGNED_KERNEL=unsigned),
                            capture_output=True,
                            text=True,
                            check=True)
    payload = as_str_mapping(load_json(result.stdout))
    packages = as_list(payload['packages'])
    return [as_str(as_mapping(package)['name']) for package in packages]


@needs_jq
def test_manifest_drops_the_twin_unless_asked() -> None:
    """Only ``no`` drops the twin; an unfiltered lookup keeps it."""
    assert _expand('no') == ['kernel']
    assert _expand('yes') == ['kernel', 'kernel-nosign']
    assert _expand('') == ['kernel', 'kernel-nosign']


def test_pipeline_wires_the_twin_from_the_both_mode() -> None:
    """L1: the pipelines carry no twin plumbing, only the signing mode."""
    builder = (WORKFLOWS / 'bootc-debs-builder.yml').read_text()
    build = (WORKFLOWS / 'bootc-build.yml').read_text()
    full = (WORKFLOWS / 'full-pipeline.yml').read_text()
    assert 'unsigned_kernel' not in full
    assert 'nosign_images' not in build
    assert "unsigned_kernel == 'yes'" not in builder
    assert "unsigned-kernel: 'no'" in builder
    assert '[ "${mode}" = both ] && unsigned_kernel=yes' not in full


def test_one_apt_suite_and_one_deb_cache() -> None:
    """The twin kernels share the suite and the cache of every other deb."""
    end = (ROOT / 'bootc-debs-end' / 'action.yml').read_text()
    script = (ROOT / 'build-deb' / 'build-deb.sh').read_text()
    assert 'trixie-nosign' not in end
    assert 'debs-cache-nosign' not in end + script
    assert 'variant_suffix' not in (WORKFLOWS / 'build-one-deb.yml').read_text()


def test_signing_flag_only_hashes_the_packages_that_sign() -> None:
    """MODULE_SIGNING_ACTIVE reaches the hash of the signing packages only."""
    text = (WORKFLOWS / 'build-one-deb.yml').read_text()
    assert "steps.def.outputs.signs == 'yes'" in text
    definition = (ROOT / 'pkg-def' / 'pkg-def.sh').read_text()
    assert "grep -q 'kernel_signing_setup'" in definition


@needs_jq
def test_arches_label_limits_an_image_to_its_architectures(
    tmp_path: Path
) -> None:
    """An image labelled amd64-only has no arm64 entry and a one-arch manifest.

    It stays in the amd64 job, after the Containerfile it derives from, so no
    extra job is added.

    Args:
        tmp_path: Scratch repository root.
    """
    result = _discover(
        tmp_path,
        nosign=True,
        extra={
            'Containerfile.vendor':
            'FROM ghcr.io/daemoncores/debian-bootc:latest_amd64\n'
            'LABEL org.daemoncores.arches="amd64"\n'
        }
    )
    amd64, arm64 = ([e['tag'] for e in g['entries']] for g in result['groups'])
    assert amd64 == ['latest', 'board', 'iso', 'vendor']
    assert 'vendor' not in arm64
    manifest = {as_str(e['tag']): e['arches'] for e in result['manifest']}
    assert manifest['vendor'] == ['amd64']
    assert manifest['latest'] == ['amd64', 'arm64']
    assert len(result['groups']) == 2


@needs_jq
def test_arches_label_with_no_known_architecture_fails(tmp_path: Path) -> None:
    """A label naming no supported architecture fails the discovery.

    Args:
        tmp_path: Scratch repository root.
    """
    with pytest.raises(subprocess.CalledProcessError):
        _discover(
            tmp_path,
            nosign=False,
            extra={
                'Containerfile.vendor': 'LABEL org.daemoncores.arches=riscv64\n'
            }
        )
