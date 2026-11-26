"""Unit tests for ``scripts/owed_stages.py`` (the CI failure memory)."""

import functools
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import owed_stages

SCRIPT = Path(owed_stages.__file__)


def _gh(returncode: int,
        stdout: str = '',
        stderr: str = '') -> subprocess.CompletedProcess[str]:
    """Build the result of one ``gh`` call.

    Args:
        returncode: Exit status of ``gh``.
        stdout: Standard output of ``gh``.
        stderr: Standard error of ``gh``.

    Returns:
        The completed process.
    """
    return subprocess.CompletedProcess(['gh'], returncode, stdout, stderr)


def _answering(
    answers: list[subprocess.CompletedProcess[str]]
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stand-in serving the answers in order.

    Args:
        answers: Results to return, one call each.

    Returns:
        A callable with the ``subprocess.run`` calling convention.
    """

    def run(_argv: list[str],
            **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return the next scripted ``gh`` result.

        Args:
            _argv: Arguments of the call, ignored.
            **_kwargs: Options of the call, ignored.

        Returns:
            The next scripted result.
        """
        return answers.pop(0)

    return run


def _recording_reader(
    runs: list[dict[str, object]],
    jobs: list[dict[str, object]],
    asked: list[str],
) -> Callable[[str, str], list[dict[str, object]]]:
    """Build a ``gh_json`` stand-in returning fixed records.

    Args:
        runs: Records of the runs endpoint.
        jobs: Records of the jobs endpoint.
        asked: Receives each requested path.

    Returns:
        A callable with the ``gh_json`` calling convention.
    """

    def read(path: str, _jq: str) -> list[dict[str, object]]:
        """Answer one endpoint with its fixed records.

        Args:
            path: API path of the call.
            _jq: jq filter of the call, ignored.

        Returns:
            The records of the endpoint named in ``path``.
        """
        asked.append(path)
        return jobs if path.endswith('/jobs?per_page=100') else runs

    return read


def _raise(message: str, *args: object) -> NoReturn:
    """Fail the way the GitHub API does on an outage.

    Args:
        message: Text of the error.
        *args: Arguments of the fetch call, ignored.

    Raises:
        RuntimeError: Always, simulating the API error.
    """
    raise RuntimeError(message)


def _fixed_history(
    runs: list[tuple[str, list[owed_stages.Job]]], *_args: object
) -> list[tuple[str, list[owed_stages.Job]]]:
    """Return a fixed run history, ignoring the fetch arguments.

    Args:
        runs: History to return.
        *_args: Arguments of the fetch call, ignored.

    Returns:
        The given history.
    """
    return runs


def job(name: str, conclusion: str, step: str = '') -> owed_stages.Job:
    """Build a job as the history reports it.

    Args:
        name: Job name without the caller prefix.
        conclusion: Job conclusion.
        step: Name of a failed step, if any.

    Returns:
        The job.
    """
    steps = (owed_stages.Step(step, 'failure'), ) if step else ()
    return owed_stages.Job(f'run / {name}', conclusion, steps)


def test_failed_stage_stays_owed_through_a_green_run_that_skipped_it() -> None:
    """The bug: a later green run that skipped the ISO hid its failure."""
    runs = [
        (
            'r3', [
                job('Build ISO / build-iso', 'skipped'),
                job('Build container images (amd64)', 'success')
            ]
        ),
        ('r2', [job('Build ISO / build-iso', 'failure', 'Smoke boot')]),
        ('r1', [job('Build ISO / build-iso', 'success')]),
    ]
    found = owed_stages.owed(runs)
    assert list(found) == ['iso']
    assert found['iso'].url == 'r2'
    assert found['iso'].step == 'Smoke boot'


def test_stage_is_settled_by_a_later_green_run() -> None:
    """A green run of the stage clears the debt."""
    runs = [
        ('r2', [job('Build ISO / build-iso', 'success')]),
        ('r1', [job('Build ISO / build-iso', 'failure')]),
    ]
    assert not owed_stages.owed(runs)


def test_cancelled_counts_as_unproven() -> None:
    """A run cancelled by a newer push proved nothing."""
    runs = [('r1', [job('Build .deb packages (amd64)', 'cancelled')])]
    assert list(owed_stages.owed(runs)) == ['debs']


def test_one_red_job_among_green_ones_owes_the_stage() -> None:
    """One failing arch is enough to owe the whole stage."""
    runs = [(
        'r1', [
            job('Build .deb packages (amd64)', 'success'),
            job('Build .deb packages (arm64)', 'failure')
        ]
    )]
    assert owed_stages.owed(runs)['debs'].job == (
        'run / Build .deb packages (arm64)'
    )


def test_untracked_jobs_are_ignored() -> None:
    """Jobs outside the tracked stages never owe anything."""
    runs = [('r1', [job('Compute pipeline plan', 'failure')])]
    assert not owed_stages.owed(runs)


def test_unreadable_history_fails_instead_of_reading_clean(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """An API error exits 1: unknown history is never a clean one.

    Args:
        capsys: Output capture.
    """
    code = owed_stages.main([
        '--repo', 'o/r', '--workflow', 'w.yml', '--branch', 'main'
    ],
                            fetch=functools.partial(_raise, 'gh api: HTTP 403'))
    assert code == 1
    assert 'HTTP 403' in capsys.readouterr().err


def test_output_line_carries_the_evidence(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """Each owed stage prints stage, run, job and failed step.

    Args:
        capsys: Output capture.
    """
    history = [('u', [job('Build ISO / x', 'failure', 'Smoke')])]
    _ = owed_stages.main([
        '--repo', 'o/r', '--workflow', 'w.yml', '--branch', 'm'
    ],
                         fetch=functools.partial(_fixed_history, history))
    assert capsys.readouterr().out == 'iso\tu\trun / Build ISO / x\tSmoke\n'


def test_all_if_unknown_forces_every_stage_instead_of_failing(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead API must not turn the pipeline into a run that skips everything.

    Run 45160784394: one ``stream error`` on the jobs endpoint made the plan
    job exit 1, so every stage was skipped. Forcing every stage is still
    "unknown history is never clean", and it costs CI time, not a dead run.

    Args:
        capsys: Output capture.
    """
    url = 'repos/o/r/actions/runs/1/jobs?per_page=100'

    code = owed_stages.main(
        [
            '--repo', 'o/r', '--workflow', 'w.yml', '--branch', 'main',
            '--all-if-unknown'
        ],
        fetch=functools.partial(
            _raise, f'gh api {url}: stream error: stream ID 1; CANCEL'
        )
    )
    out = capsys.readouterr()
    assert code == 0
    assert out.out.splitlines() == [f'{s}\t\t\t' for s in owed_stages.STAGES]
    assert 'stream error' in out.err


def test_failed_step_ignores_steps_that_succeeded() -> None:
    """A failed job whose steps all succeeded has no step to name."""
    steps = (
        owed_stages.Step('Checkout', 'success'),
        owed_stages.Step('Build', 'success'),
    )
    job_without_bad_step = owed_stages.Job(
        'run / Build ISO / x', 'failure', steps
    )
    assert owed_stages.failed_step(job_without_bad_step) == ''


def test_a_second_red_job_keeps_the_first_evidence() -> None:
    """The first failing job of a stage stays the evidence reported."""
    runs = [(
        'u', [
            job('Build .deb packages (amd64)', 'failure', 'Configure'),
            job('Build .deb packages (arm64)', 'failure', 'Build'),
        ]
    )]
    found = owed_stages.owed(runs)
    assert found['debs'].job == 'run / Build .deb packages (amd64)'
    assert found['debs'].step == 'Configure'


def test_the_walk_stops_once_every_stage_is_settled() -> None:
    """No older run is read once all six stages have a verdict."""
    green = [
        job('Rebuild the deb-builder env image', 'success'),
        job('Build .deb packages (amd64)', 'success'),
        job('Build container images (amd64)', 'success'),
        job('Build ISO (amd64)', 'success'),
        job('Build rpi .img (arm64)', 'success'),
        job('Build gallium .img (arm64)', 'success'),
    ]
    runs = [('r2', green), ('r1', [job('Build ISO / build-iso', 'failure')])]
    assert not owed_stages.owed(runs)


def test_gh_json_parses_one_json_object_per_line(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parsed records of a successful call are returned in order.

    Args:
        monkeypatch: Environment patcher.
    """
    monkeypatch.setattr(
        subprocess, 'run', _answering([_gh(0, '{"id": 1}\n{"id": 2}\n\n')])
    )
    assert owed_stages.gh_json('repos/o/r/runs', '.workflow_runs[]') == [{
        'id': 1
    }, {
        'id': 2
    }]


def test_gh_json_retries_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5xx is retried after a delay instead of reading the history empty.

    Args:
        monkeypatch: Environment patcher.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(
        subprocess, 'run',
        _answering([_gh(1, '', 'HTTP 502'),
                    _gh(0, '{"id": 1}\n')])
    )
    monkeypatch.setattr(owed_stages.time, 'sleep', sleeps.append)
    assert owed_stages.gh_json('repos/o/r/runs', '.workflow_runs[]') == [{
        'id': 1
    }]
    assert sleeps == [owed_stages.RETRY_DELAY]


def test_gh_json_raises_after_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead API raises: an unknown history is never read as clean.

    Args:
        monkeypatch: Environment patcher.
    """
    sleeps: list[float] = []
    monkeypatch.setattr(
        subprocess, 'run',
        _answering([_gh(1, '', 'stream error')] * owed_stages.ATTEMPTS)
    )
    monkeypatch.setattr(owed_stages.time, 'sleep', sleeps.append)
    with pytest.raises(RuntimeError, match='stream error'):
        _ = owed_stages.gh_json('repos/o/r/runs', '.workflow_runs[]')
    assert sleeps == [
        owed_stages.RETRY_DELAY,
        2 * owed_stages.RETRY_DELAY,
        3 * owed_stages.RETRY_DELAY,
    ]


def test_history_reads_the_jobs_of_each_listed_run(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs are sliced to the limit and each one brings its jobs.

    Args:
        monkeypatch: Environment patcher.
    """
    asked: list[str] = []
    runs: list[dict[str, object]] = [
        {
            'id': 7,
            'html_url': 'u7'
        },
        {
            'id': 8,
            'html_url': 'u8'
        },
    ]
    jobs: list[dict[str, object]] = [
        {
            'name': 'run / Build ISO / build-iso',
            'conclusion': 'failure',
            'steps': [{
                'name': 'Smoke',
                'conclusion': 'failure'
            }],
        },
        {
            'name': 'run / Compute plan',
            'conclusion': '',
            'steps': []
        },
    ]

    monkeypatch.setattr(
        owed_stages, 'gh_json', _recording_reader(runs, jobs, asked)
    )
    got = list(owed_stages.history('o/r', 'w.yml', 'main', 1))
    assert got == [(
        'u7', [
            owed_stages.Job(
                'run / Build ISO / build-iso', 'failure',
                (owed_stages.Step('Smoke', 'failure'), )
            ),
            owed_stages.Job('run / Compute plan', '', ()),
        ]
    )]
    assert asked == [
        'repos/o/r/actions/workflows/w.yml/runs' +
        '?branch=main&status=completed&per_page=1',
        'repos/o/r/actions/runs/7/jobs?per_page=100',
    ]


def test_the_script_entry_point_requires_its_arguments(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run as ``__main__`` with no argument, argparse exits 2.

    Args:
        monkeypatch: Environment patcher.
    """
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT)])
    with pytest.raises(SystemExit) as excinfo:
        _ = runpy.run_path(str(SCRIPT), run_name='__main__')
    assert excinfo.value.code == 2
