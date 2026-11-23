"""Unit tests for ``scripts/owed_stages.py`` (the CI failure memory)."""

import functools
import sys
from pathlib import Path
from typing import NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import owed_stages


def _raise(message: str, *args: object) -> NoReturn:
    """Fail the way the GitHub API does on an outage.

    Args:
        message: Text of the error.
        *args: Arguments of the fetch call, ignored.

    Raises:
        RuntimeError: Always, simulating the API error.
    """
    raise RuntimeError(message)


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
    owed_stages.main(['--repo', 'o/r', '--workflow', 'w.yml', '--branch', 'm'],
                     fetch=lambda *_: history)
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
