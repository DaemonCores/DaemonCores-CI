"""owed_stages.py - pipeline stages the previous runs left unproven.

Usage:
    python3 owed_stages.py --repo OWNER/REPO --workflow FILE --branch BRANCH
                           [--limit N] [--all-if-unknown]

A green run proves nothing about a stage it skipped. This walks the completed
runs of the caller workflow on one branch, newest first, and for each stage
(env, debs, image, iso, img_rpi, img_gallium) keeps the verdict of the latest
run where that stage really ran: a failed or cancelled job makes the stage
*owed* (the next run must run it again), a fully green stage is settled. A run
that skipped the stage says nothing and the walk continues to the older run.

The only state is the GitHub run history itself; nothing is stored. One line
per owed stage is printed on stdout::

    STAGE<TAB>RUN_URL<TAB>JOB<TAB>FAILED_STEP

and nothing is printed when no stage is owed. A failed API call exits non-zero:
an unknown history must never read as a clean one. With ``--all-if-unknown``
the same failure prints every stage instead (empty run column): an unreadable
history then means "run everything", which is the other way never to read an
unknown history as a clean one, and it keeps a GitHub network error from
turning the whole pipeline into a run that skips every stage.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import cast

# Stage -> pattern of the job names (without the "run / " caller prefix).
STAGES: dict[str, re.Pattern[str]] = {
    'env':
    re.compile(r'^Rebuild the deb-builder env image'),
    'debs':
    re.compile(
        r'^(Build \.deb packages|Publish \.deb packages|Aggregate debs outputs)'
    ),
    'image':
    re.compile(r'^Build container images'),
    'iso':
    re.compile(r'^Build ISO'),
    'img_rpi':
    re.compile(r'^Build rpi \.img'),
    'img_gallium':
    re.compile(r'^Build gallium \.img'),
}

ATTEMPTS = 4
RETRY_DELAY = 5

BAD = frozenset({'failure', 'cancelled', 'timed_out', 'startup_failure'})


@dataclass(frozen=True)
class Step:
    """One step of a job.

    Attributes:
        name: Step name.
        conclusion: Step conclusion (empty when not finished).
    """

    name: str
    conclusion: str


@dataclass(frozen=True)
class Job:
    """One job of a run.

    Attributes:
        name: Job name as the API reports it.
        conclusion: Job conclusion (empty when not finished).
        steps: Its steps.
    """

    name: str
    conclusion: str
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """Where a stage was last seen broken.

    Attributes:
        url: Run page.
        job: Name of a job that did not succeed.
        step: Name of the step that failed in that job (empty if unknown).
    """

    url: str
    job: str
    step: str


Run = tuple[str, list[Job]]
Fetch = Callable[[str, str, str, int], Iterable[Run]]


def stage_of(job_name: str) -> str | None:
    """Map a job name to its stage.

    Args:
        job_name: Job name as the API reports it (``run / Build ISO / x``).

    Returns:
        The stage name, or ``None`` for a job that belongs to no tracked stage.
    """
    bare = job_name.split(' / ', 1)[-1]
    for stage, pattern in STAGES.items():
        if pattern.search(bare):
            return stage
    return None


def failed_step(job: Job) -> str:
    """Name the first step of a job that did not succeed.

    Args:
        job: Job to inspect.

    Returns:
        The step name, or an empty string when no step is marked bad.
    """
    for step in job.steps:
        if step.conclusion in BAD:
            return step.name
    return ''


def run_verdicts(jobs: Iterable[Job], url: str) -> dict[str, Evidence | None]:
    """Settle each stage a run actually ran.

    Args:
        jobs: Jobs of one run.
        url: Page of that run.

    Returns:
        Stage -> ``None`` when it ran green, an ``Evidence`` when a job of it
        failed or was cancelled. Stages the run skipped are absent.
    """
    verdicts: dict[str, Evidence | None] = {}
    for job in jobs:
        stage = stage_of(job.name)
        if stage is None or job.conclusion in ('', 'skipped', 'neutral'):
            continue
        if job.conclusion in BAD:
            if verdicts.get(stage) is None:
                verdicts[stage] = Evidence(url, job.name, failed_step(job))
        else:
            verdicts.setdefault(stage, None)
    return verdicts


def owed(runs: Iterable[Run]) -> dict[str, Evidence]:
    """Find the stages whose latest real run was not green.

    Args:
        runs: ``(run url, jobs)`` of the completed runs, newest first. The
            iterable is consumed lazily and abandoned once every stage is
            settled.

    Returns:
        Owed stage -> evidence of the break.
    """
    settled: dict[str, Evidence | None] = {}
    for url, jobs in runs:
        for stage, verdict in run_verdicts(jobs, url).items():
            settled.setdefault(stage, verdict)
        if len(settled) == len(STAGES):
            break
    return {s: e for s, e in settled.items() if e is not None}


def gh_json(path: str, jq: str) -> list[dict[str, object]]:
    """Call the GitHub API through ``gh`` and parse the JSON lines.

    Args:
        path: API path with query string.
        jq: jq filter that prints one JSON object per line.

    Returns:
        The parsed objects.

    Raises:
        RuntimeError: When ``gh`` still fails after the retries (history
            unknown, never read as clean).
    """
    stderr = ''
    for attempt in range(ATTEMPTS):
        done = subprocess.run(['gh', 'api', '--paginate', path, '--jq', jq],
                              capture_output=True,
                              text=True,
                              check=False)
        if done.returncode == 0:
            return [
                cast(dict[str, object], json.loads(line))
                for line in done.stdout.splitlines() if line
            ]
        stderr = done.stderr.strip()
        # GitHub answers 5xx now and then: wait, then ask again.
        if attempt + 1 < ATTEMPTS:
            time.sleep(RETRY_DELAY * (attempt + 1))
    raise RuntimeError(f'gh api {path}: {stderr}')


def history(repo: str, workflow: str, branch: str, limit: int) -> Iterator[Run]:
    """Fetch the completed runs, newest first, jobs only when asked for.

    Args:
        repo: ``OWNER/REPO``.
        workflow: Workflow file name of the caller.
        branch: Branch the runs ran on.
        limit: Number of runs to look at at most.

    Yields:
        Run: One ``(url, jobs)`` tuple per run.
    """
    runs = gh_json(
        f'repos/{repo}/actions/workflows/{workflow}/runs'
        f'?branch={branch}&status=completed&per_page={limit}',
        '.workflow_runs[] | {id, html_url}'
    )
    for run in runs[:limit]:
        raw = gh_json(
            f'repos/{repo}/actions/runs/{run["id"]}/jobs?per_page=100',
            '.jobs[] | {name, conclusion: (.conclusion // ""), steps: '
            '[.steps[]? | {name, conclusion: (.conclusion // "")}]}'
        )
        jobs = [
            Job(
                str(j['name']), str(j['conclusion']),
                tuple(
                    Step(str(st['name']), str(st['conclusion']))
                    for st in cast(list[dict[str, object]], j['steps'])
                )
            ) for j in raw
        ]
        yield str(run['html_url']), jobs


def main(argv: list[str] | None = None, fetch: Fetch = history) -> int:
    """Print the owed stages.

    Args:
        argv: Arguments, ``sys.argv[1:]`` when omitted.
        fetch: Run-history source (replaced in tests).

    Returns:
        Process exit code: 0, or 1 when the history could not be read.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--workflow', required=True)
    parser.add_argument('--branch', required=True)
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument(
        '--all-if-unknown',
        action='store_true',
        help='print every stage when the history cannot be read, instead of '
        'failing'
    )
    args = parser.parse_args(argv)
    repo, workflow = cast(str, args.repo), cast(str, args.workflow)
    branch, limit = cast(str, args.branch), cast(int, args.limit)
    all_if_unknown = cast(bool, args.all_if_unknown)
    try:
        found = owed(fetch(repo, workflow, branch, limit))
    except RuntimeError as err:
        if not all_if_unknown:
            print(err, file=sys.stderr)
            return 1
        # Conservative reading of an unknown history: every stage is owed.
        # Failing instead took the whole run down (run 45160784394: the plan
        # job died on a GitHub stream error and every stage was skipped).
        print(
            f'{err}\n'
            f'history unreadable: forcing every stage: {", ".join(STAGES)}',
            file=sys.stderr
        )
        for stage in STAGES:
            print(f'{stage}\t\t\t')
        return 0
    for stage, ev in found.items():
        print(f'{stage}\t{ev.url}\t{ev.job}\t{ev.step}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
