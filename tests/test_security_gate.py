"""Unit tests for the automatic security gates of the election.

Every HTTP consumer of ``upstream-track/security_gate.sh`` is fed through a
fixture file (``GATE_OSV_FIXTURE``, ``GATE_KEV_FIXTURE``,
``GATE_CVE_FEED_FIXTURE``) or a ``file://`` ``GITHUB_API_URL`` tree: the tests
never touch the network.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

CI = Path(__file__).resolve().parents[1]
GATE = CI / 'upstream-track' / 'security_gate.sh'
TRACK = CI / 'upstream-track' / 'track.sh'
SBOM = CI / 'bootc-debs-end' / 'sbom.sh'
CONTENT_HASH = CI / 'build-deb' / 'content-hash.sh'
DIGEST = 'a' * 64


@pytest.fixture(name='election')
def fixture_election(tmp_path: Path) -> Path:
    """An empty election log directory.

    Args:
        tmp_path: Pytest scratch directory.

    Returns:
        The election log directory.
    """
    d = tmp_path / 'election'
    d.mkdir()
    return d


def _gate(snippet: str,
          election: Path | None = None,
          **extra: str) -> subprocess.CompletedProcess[str]:
    """Source the gate library and run the given bash lines.

    Args:
        snippet: The bash statements after the source.
        election: The election log directory (a fixture or None).
        **extra: Additional environment variables.

    Returns:
        The completed process.
    """
    env = {'PATH': os.environ['PATH']}
    if election is not None:
        env['ELECTION_DIR'] = str(election)
    env.update(extra)
    return subprocess.run(['bash', '-c', f"source '{GATE}'; {snippet}"],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=60)


def _log_events(election: Path) -> list[dict]:
    """Parse the JSONL selection.log of an election log directory.

    Args:
        election: Election log directory.

    Returns:
        One dict per line.
    """
    text = (election / 'selection.log').read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_care_delay_holds_a_fresh_candidate(election: Path) -> None:
    """A candidate younger than the care delay is held, the log says why.

    Args:
        election: Election log directory.
    """
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    p = _gate(
        f"gate_care_delay {now} 'kernel 7.2.16'",
        election,
        CARE_DELAY='86400',
        PKG='k'
    )
    assert p.returncode == 3
    assert 'care-delay HOLD' in p.stderr
    assert 'CARE_DELAY 86400' in p.stderr
    holds = [x for x in _log_events(election) if x['event'] == 'hold']
    assert holds
    assert holds[0]['care'] == 'hold'


def test_care_delay_discharges_for_the_hotfix(election: Path) -> None:
    """CARE_DELAY=0 passes a fresh candidate and logs the hotfix override.

    Args:
        election: Election log directory.
    """
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    p = _gate(
        f"gate_care_delay {now} 'kernel 7.2.16'",
        election,
        CARE_DELAY='0',
        PKG='k'
    )
    assert p.returncode == 0
    assert 'CARE_DELAY=0, hotfix' in p.stderr


def test_care_delay_passes_an_old_candidate(election: Path) -> None:
    """A candidate older than the delay passes with the age cited.

    Args:
        election: Election log directory.
    """
    old = '2020-01-01T00:00:00Z'
    p = _gate(f"gate_care_delay {old} 'kernel 7.2.15'", election, PKG='k')
    assert p.returncode == 0
    assert 'care-delay pass' in p.stderr


def test_osv_open_advisory_holds_the_election(
    tmp_path: Path, election: Path
) -> None:
    """An OSV response carrying vulns holds; the ids land in the log.

    Args:
        tmp_path: Pytest scratch directory.
        election: Election log directory.
    """
    fixture = tmp_path / 'osv.json'
    fixture.write_text(
        json.dumps({
            'vulns': [{
                'id': 'GHSA-xxxx-yyyy-zzzz'
            }, {
                'id': 'CVE-2026-1234'
            }]
        })
    )
    p = _gate(
        """gate_osv '{\"commit\": \"1\"}'""",
        election,
        GATE_OSV_FIXTURE=str(fixture),
        PKG='r'
    )
    assert p.returncode == 3
    assert 'OSV HOLD' in p.stderr
    assert 'GHSA-xxxx-yyyy-zzzz' in p.stderr
    assert any(x.get('osv') for x in _log_events(election))


def test_osv_empty_response_passes(tmp_path: Path, election: Path) -> None:
    """An empty OSV response passes the flux.

    Args:
        tmp_path: Pytest scratch directory.
        election: Election log directory.
    """
    fixture = tmp_path / 'osv.json'
    fixture.write_text('{}')
    p = _gate(
        """gate_osv '{\"commit\": \"1\"}'""",
        election,
        GATE_OSV_FIXTURE=str(fixture),
        PKG='r'
    )
    assert p.returncode == 0


def test_flux_unreachable_source_passes_loud(election: Path) -> None:
    """A dead advisory source is a loud logged pass, never a blind hold.

    Args:
        election: Election log directory.
    """
    p = _gate(
        "fetch_api() { return 1; }; gate_flux '' acme/tool", election, PKG='r'
    )
    assert p.returncode == 0
    assert 'unreachable' in p.stderr
    events = _log_events(election)
    assert any(x.get('flux') == 'unreachable' for x in events)


def test_ghsa_published_advisory_holds_the_source(
    tmp_path: Path, election: Path
) -> None:
    """A published GHSA advisory on the source repository holds.

    Args:
        tmp_path: Pytest scratch directory.
        election: Election log directory.
    """
    api = tmp_path / 'api'
    adv = api / 'repos' / 'acme' / 'tool'
    adv.mkdir(parents=True)
    (adv / 'security-advisories').write_text(
        json.dumps([{
            'ghsa_id': 'GHSA-aaaa-bbbb-cccc',
            'state': 'published'
        }])
    )
    p = _gate(
        "fetch_api() { curl -fsSL "
        "'file://" + str(adv) + "/security-advisories'; }; "
        "gate_flux '' acme/tool",
        election,
        PKG='r'
    )
    assert p.returncode == 3
    assert 'GHSA HOLD' in p.stderr
    assert 'GHSA-aaaa-bbbb-cccc' in p.stderr


def _kev_fixture(tmp_path: Path, linux: bool) -> Path:
    """A CISA KEV feed fixture with (or without) a Linux kernel entry.

    Args:
        tmp_path: Pytest scratch directory.
        linux: Whether the feed lists a Linux kernel CVE.

    Returns:
        The fixture path.
    """
    vulns = [{
        'cveID': 'CVE-2024-0001',
        'vendorProject': 'Chrome',
        'product': 'Chromium'
    }]
    if linux:
        vulns.insert(
            0, {
                'cveID': 'CVE-2026-9999',
                'vendorProject': 'Linux',
                'product': 'Linux Kernel'
            }
        )
    fixture = tmp_path / 'kev.json'
    fixture.write_text(json.dumps({'vulnerabilities': vulns}))
    return fixture


def test_kev_bypass_lifts_the_delay(tmp_path: Path, election: Path) -> None:
    """A kernel CVE in CISA KEV bypasses, the log cites the CVE id.

    Args:
        tmp_path: Pytest scratch directory.
        election: Election log directory.
    """
    p = _gate(
        'gate_bypass; echo bypass-rc:$?',
        election,
        GATE_KEV_FIXTURE=str(_kev_fixture(tmp_path, True)),
        GATE_CVE_FEED_FIXTURE='/dev/null',
        PKG='k'
    )
    assert 'bypass-rc:0' in p.stdout
    assert 'care-delay BYPASS: CISA KEV' in p.stderr
    assert 'CVE-2026-9999' in p.stderr
    assert any(x.get('kev') for x in _log_events(election))


def test_no_bypass_when_the_feeds_stay_quiet(
    tmp_path: Path, election: Path
) -> None:
    """A quiet KEV feed and a HIGH announce = no bypass.

    Args:
        tmp_path: Pytest scratch directory.
        election: Election log directory.
    """
    feed = tmp_path / 'feed.atom'
    feed.write_text(
        '<entry><title>CVE-2026-1111: net: x</title>'
        '<content>severity rating: HIGH</content></entry>'
    )
    p = _gate(
        'gate_bypass; echo rc:$?',
        election,
        GATE_KEV_FIXTURE=str(_kev_fixture(tmp_path, False)),
        GATE_CVE_FEED_FIXTURE=str(feed),
        PKG='k'
    )
    assert 'rc:1' in p.stdout


def test_cve_announce_critical_cve_bypasses(
    tmp_path: Path, election: Path
) -> None:
    """A CRITICAL linux-cve-announce entry carries the bypass.

    Args:
        tmp_path: Pytest scratch directory.
        election: Election log directory.
    """
    feed = tmp_path / 'feed.atom'
    feed.write_text(
        '<entry><title>CVE-2026-1234: mm: a bad one</title>'
        '<content>p: kernel CRITICAL severity</content></entry>'
    )
    p = _gate(
        'gate_bypass; echo rc:$?',
        election,
        GATE_KEV_FIXTURE=str(tmp_path / 'absent-kev.json'),
        GATE_CVE_FEED_FIXTURE=str(feed),
        PKG='k'
    )
    assert 'rc:0' in p.stdout
    assert 'CVE-2026-1234' in p.stderr
    assert 'linux-cve-announce' in p.stderr


def test_record_materializes_the_selection_log(election: Path) -> None:
    """The election record files are the publish SBOM source.

    Args:
        election: Election log directory.
    """
    snippet = "gate_record KERNEL_VER kernel 7.2.15 https://x/linux.tar.xz " \
              "'aabbcc' '2026-11-01' pass pass\n" \
              "gate_record bore sibling 0000abcd https://lore/x/raw " \
              "'ddee' '' bypass pass"
    p = _gate(snippet, election, PKG='k')
    assert p.returncode == 0, p.stderr
    record = json.loads((election / 'election-k.json').read_text())
    entries = {e['key']: e for e in record['entries']}
    assert entries['KERNEL_VER']['kind'] == 'kernel'
    assert entries['KERNEL_VER']['sha256'] == 'aabbcc'
    assert entries['bore']['care_delay'] == 'bypass'


def test_hold_issue_is_a_noop_without_a_token(election: Path) -> None:
    """Without a GH token the hold-issue opening is skipped, never fatal.

    Args:
        election: Election log directory.
    """
    p = _gate(
        "gate_hold_issue 'Security hold: x' 'body'",
        election,
        PKG='k',
        GH_TOKEN=''
    )
    assert p.returncode == 0


@pytest.fixture(name='track')
def fixture_track(tmp_path: Path) -> Path:
    """The track.sh fixture tree of the head-package tests.

    One rolling head package whose commit date (and its ``file://`` API
    documents) the tests rewrite.

    Args:
        tmp_path: Pytest scratch directory.

    Returns:
        The API tree root.
    """
    api = tmp_path / 'api'
    man = api / 'packages.yml'
    man.parent.mkdir(parents=True)
    man.write_text(
        'packages:\n  - name: rolling\n    track:\n'
        '      ROLL_COMMIT:\n        repo: acme/rolling\n'
        '        head: true\n'
    )
    commit = api / 'repos' / 'acme' / 'rolling' / 'commits'
    commit.mkdir(parents=True)
    head_doc = {
        'sha': '0123456789abcdef0123456789abcdef01234567',
        'commit': {
            'committer': {
                'date': '2020-01-01T00:00:00Z'
            }
        }
    }
    (commit / 'HEAD').write_text(json.dumps(head_doc))
    (commit / head_doc['sha']).write_text(json.dumps(head_doc))
    advisories = api / 'repos' / 'acme' / 'rolling' / 'security-advisories'
    advisories.parent.mkdir(parents=True, exist_ok=True)
    advisories.write_text(json.dumps([]))
    return api


def _track_run(api: Path, date: str, **extra: str) \
        -> subprocess.CompletedProcess[str]:
    """Run track.sh resolve for the rolling fixture with a commit date.

    Args:
        api: The fixture API tree.
        date: The commit date the API documents carry.
        **extra: Extra environment (CARE_DELAY, gate fixtures).

    Returns:
        The completed process.
    """
    head = api / 'repos' / 'acme' / 'rolling' / 'commits' / 'HEAD'
    body = json.loads(head.read_text())
    body['commit']['committer']['date'] = date
    head.write_text(json.dumps(body))
    (head.parent / body['sha']).write_text(json.dumps(body))
    env = {
        'PATH': os.environ['PATH'],
        'MAN': str(api / 'packages.yml'),
        'GITHUB_API_URL': f'file://{api}',
        'GITHUB_OUTPUT': str(api / 'out'),
        'PKG': 'rolling',
        'ELECTION_DIR': str(api / 'election'),
        **extra,
    }
    return subprocess.run(['bash', str(TRACK), 'resolve'],
                          check=False,
                          capture_output=True,
                          text=True,
                          env=env,
                          timeout=60)


def test_track_holds_a_young_head_commit(track: Path) -> None:
    """A head commit younger than CARE_DELAY is held: skip + logged reason.

    Args:
        track: The track fixture API tree.
    """
    api = track
    result = _track_run(api, '2026-11-10T00:00:00Z', CARE_DELAY='2592000')
    assert result.returncode == 0, result.stderr
    out = (api / 'out').read_text()
    assert 'skip=true' in out
    assert 'ROLL_COMMIT=' not in out.split('TRACK_ENV_EOF')[0]
    assert 'security HOLD' in result.stderr
    holds = [x for x in _log_events(api / 'election') if x['event'] == 'hold']
    assert holds
    assert holds[0]['care'] == 'hold'


def test_track_holds_an_open_advisory(track: Path) -> None:
    """An open advisory on the source holds even an old commit.

    Args:
        track: The track fixture API tree.
    """
    api = track
    adv = api / 'repos' / 'acme' / 'rolling' / 'security-advisories'
    adv.write_text(
        json.dumps([{
            'ghsa_id': 'GHSA-dddd-eeee-ffff',
            'state': 'published'
        }])
    )
    result = _track_run(api, '2020-01-01T00:00:00Z')
    assert result.returncode == 0, result.stderr
    assert 'skip=true' in (api / 'out').read_text()
    assert 'GHSA HOLD' in result.stderr
    assert 'ROLL_COMMIT=' not in (api / 'out').read_text() \
        .split('TRACK_ENV_EOF')[0]


def test_track_passes_an_old_head_commit(track: Path) -> None:
    """An old head commit resolves; the record + selection.log carry it.

    Args:
        track: The track fixture API tree.
    """
    api = track
    result = _track_run(api, '2020-01-01T00:00:00Z')
    assert result.returncode == 0, result.stderr
    out = (api / 'out').read_text()
    assert 'ROLL_COMMIT=0123456789abcdef0123456789abcdef01234567' in out
    assert 'skip=false' in out
    assert 'care-delay pass' in result.stderr
    record = json.loads((api / 'election' / 'election-rolling.json').read_text()
                        )
    entry = record['entries'][0]
    assert entry['kind'] == 'commit'
    assert entry['id'] == '0123456789abcdef0123456789abcdef01234567'
    assert entry['care_delay'] == 'pass'


def test_write_meta_merges_the_election_record(tmp_path: Path) -> None:
    """The publish metadata carries the election record verbatim.

    Args:
        tmp_path: Pytest scratch directory.
    """
    record = tmp_path / 'election-pkg.json'
    record.write_text(
        json.dumps({
            'package': 'pkg',
            'entries': [{
                'key': 'K',
                'id': 'v1'
            }]
        })
    )
    debs = tmp_path / 'debs'
    debs.mkdir()
    env = {
        'PATH': os.environ['PATH'],
        'DEBS_DIR': str(debs),
        'PACKAGE': 'pkg',
        'ARCH': 'amd64',
        'ELECTION_RECORD': str(record)
    }
    p = subprocess.run([
        'bash', '-c',
        f"source '{CONTENT_HASH}'; write_meta 1234 false '[\"p.deb\"]'"
    ],
                       check=False,
                       capture_output=True,
                       text=True,
                       env=env,
                       timeout=60)
    assert p.returncode == 0, p.stderr
    meta = json.loads((debs / '.meta' / 'pkg-amd64.json').read_text())
    assert meta['election']['entries'][0]['id'] == 'v1'
    assert meta['hash'] == '1234'


def test_write_meta_without_a_record_stays_hash_only(tmp_path: Path) -> None:
    """The legacy metadata shape survives an absent election record.

    Args:
        tmp_path: Pytest scratch directory.
    """
    debs = tmp_path / 'debs'
    debs.mkdir()
    env = {
        'PATH': os.environ['PATH'],
        'DEBS_DIR': str(debs),
        'PACKAGE': 'pkg',
        'ARCH': 'amd64',
        'ELECTION_RECORD': ''
    }
    p = subprocess.run([
        'bash', '-c',
        f"source '{CONTENT_HASH}'; write_meta 1234 false '[\"p.deb\"]'"
    ],
                       check=False,
                       capture_output=True,
                       text=True,
                       env=env,
                       timeout=60)
    assert p.returncode == 0, p.stderr
    meta = json.loads((debs / '.meta' / 'pkg-amd64.json').read_text())
    assert meta == {'hash': '1234', 'skipped': False, 'debs': ['p.deb']}


def test_sbom_aggregates_and_a_moved_id_lands_in_the_changelog(
    tmp_path: Path
) -> None:
    """The publish aggregates the .meta election records.

    A moved id names the variant in the changelog, an unrecorded package
    never enters the SBOM.

    Args:
        tmp_path: Pytest scratch directory.
    """
    debs = tmp_path / 'debs' / '.meta'
    debs.mkdir(parents=True)
    out = tmp_path / 'out'
    out.mkdir()
    first = {
        'package':
        'linux-image',
        'elected_at':
        't',
        'entries': [{
            'key': 'KERNEL_VER',
            'kind': 'kernel',
            'id': '7.2.15',
            'url': 'https://u/linux.tar.xz',
            'sha256': DIGEST,
            'date': '2026-11-01',
            'care_delay': 'pass',
            'flux': 'pass'
        }]
    }
    (debs / 'linux-image-amd64.json').write_text(
        json.dumps({
            'hash': 'x',
            'skipped': False,
            'debs': ['k_7.2.15_amd64.deb'],
            'election': first
        })
    )
    (debs / 'plain-pkg.json').write_text(
        json.dumps({
            'hash': 'x',
            'skipped': False,
            'debs': ['p_1_amd64.deb']
        })
    )
    prior = {
        'packages': [{
            'package': 'linux-image',
            'arch': 'amd64',
            'debs': [],
            'election': {
                'entries': [{
                    'key': 'KERNEL_VER',
                    'kind': 'kernel',
                    'id': '7.2.14',
                    'url': '',
                    'sha256': '',
                    'date': '2026-10-01',
                    'care_delay': 'pass',
                    'flux': 'pass'
                }]
            }
        }]
    }
    p = subprocess.run(['bash', str(SBOM)],
                       check=False,
                       capture_output=True,
                       text=True,
                       env={
                           'PATH': os.environ['PATH'],
                           'DEBS_DIR': str(tmp_path / 'debs'),
                           'OUT_DIR': str(out),
                           'PRIOR_SBOM': json.dumps(prior)
                       },
                       timeout=60)
    assert p.returncode == 0, p.stderr
    sbom = json.loads((out / 'sbom.json').read_text())
    assert sbom['source'].startswith('election selection.log')
    assert [x['package'] for x in sbom['packages']] == ['linux-image']
    assert sbom['packages'][0]['election']['entries'][0]['id'] == '7.2.15'
    changelog = (out / 'CHANGELOG.md').read_text()
    assert 'Variants moved this publish' in changelog
    assert '7.2.14 -> 7.2.15' in changelog
    assert 'sha256' in changelog
    assert 'plain-pkg' not in changelog


def test_sbom_first_publish_lists_every_record(tmp_path: Path) -> None:
    """Without a prior SBOM the changelog honestly reads first publish.

    Args:
        tmp_path: Pytest scratch directory.
    """
    debs = tmp_path / 'debs' / '.meta'
    debs.mkdir(parents=True)
    out = tmp_path / 'out'
    out.mkdir()
    meta = {
        'package':
        'k',
        'elected_at':
        't',
        'entries': [{
            'key': 'KERNEL_VER',
            'kind': 'kernel',
            'id': '7.2.15',
            'url': '',
            'sha256': DIGEST,
            'date': '',
            'care_delay': 'bypass',
            'flux': 'pass'
        }]
    }
    (debs / 'k-amd64.json').write_text(
        json.dumps({
            'hash': 'x',
            'skipped': False,
            'debs': [],
            'election': meta
        })
    )
    p = subprocess.run(['bash', str(SBOM)],
                       check=False,
                       capture_output=True,
                       text=True,
                       env={
                           'PATH': os.environ['PATH'],
                           'DEBS_DIR': str(tmp_path / 'debs'),
                           'OUT_DIR': str(out)
                       },
                       timeout=60)
    assert p.returncode == 0, p.stderr
    changelog = (out / 'CHANGELOG.md').read_text()
    assert 'First published SBOM' in changelog
    assert '7.2.15' in changelog
    assert 'bypass' in changelog
