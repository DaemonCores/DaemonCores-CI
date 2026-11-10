"""Unit tests for the issue-writing steps of ``build-one-deb.yml``.

The two inline steps (``Flag broken build`` / ``Close broken-build issue on
success``) are extracted from the live workflow and run against a fake GitHub
Issues API, so the tests pin the outward behaviour the owner demands from the
CI: one English issue per broken package, no per-run comment noise (the body
is refreshed instead), and a closed issue with a single comment on recovery.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import issues_api
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github' / 'workflows' / 'build-one-deb.yml'

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ('jq', 'curl', 'bash'))
    or not WORKFLOW.exists(),
    reason='jq, curl and the workflow file are required'
)


def _step_run(step_name: str) -> str:
    """Extract the run block of one step of the build job.

    Args:
        step_name: Exact step name in the workflow.

    Returns:
        The value of the step's ``run:`` entry.
    """
    text = cast(dict[str, Any], yaml.safe_load(WORKFLOW.read_text()))
    steps = text['jobs']['build']['steps']
    step = next(item for item in steps if item.get('name') == step_name)
    return str(step['run'])


def _run_step(script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run one extracted step script with the fake-API environment.

    Args:
        script: Bash source of the step.
        env: Environment, see :func:`issues_api.actions_env`.

    Returns:
        The completed process.
    """
    base_env = dict(os.environ)
    base_env.update(env)
    return subprocess.run(['bash'],
                          input=script,
                          env=base_env,
                          capture_output=True,
                          text=True,
                          check=False)


def test_flag_step_refreshes_the_existing_issue_without_commenting() -> None:
    """A repeat failure PATCHes the issue body; no comment is appended."""
    api = issues_api.FakeIssuesApi(
        open_issues=[issues_api.broken_issue(7, 'linux-modules-nvidia')]
    )
    try:
        done = _run_step(
            _step_run('Flag broken build'),
            issues_api.actions_env(api, 'linux-modules-nvidia')
        )
        assert done.returncode == 0, done.stderr
        patches = api.calls('PATCH', '/repos/o/r/issues/7')
        assert len(patches) == 1, api.requests
        assert 'title' not in patches[0]['data']
        body = patches[0]['data']['body']
        assert 'build failed (compilation or post-patch content check)' in body
        assert 'Run: http://127.0.0.1:9/o/r/actions/runs/42' in body
        assert api.calls('POST', '/comments') == []
        assert api.calls('POST', '/repos/o/r/issues') == []
    finally:
        api.stop()


def test_flag_step_opens_one_english_issue_when_none_exists() -> None:
    """The first failure creates exactly one issue with an English body."""
    api = issues_api.FakeIssuesApi()
    try:
        done = _run_step(
            _step_run('Flag broken build'),
            issues_api.actions_env(api, 'linux-modules-nvidia')
        )
        assert done.returncode == 0, done.stderr
        created = api.calls('POST', '/repos/o/r/issues')
        assert len(created) == 1, api.requests
        assert created[0]['data']['title'] == \
            '⚠️ Broken build: linux-modules-nvidia (last valid .deb reused)'
        body = created[0]['data']['body']
        assert 'last published .deb was reused' in body
        assert 'cache hash is left unchanged' in body
        assert api.calls('PATCH', '/issues') == []
    finally:
        api.stop()


def test_close_step_comments_once_and_closes_the_issue() -> None:
    """Recovery posts a single English comment and closes the issue."""
    api = issues_api.FakeIssuesApi(
        open_issues=[issues_api.broken_issue(3, 'linux-modules-nvidia')]
    )
    try:
        done = _run_step(
            _step_run('Close broken-build issue on success'),
            issues_api.actions_env(api, 'linux-modules-nvidia')
        )
        assert done.returncode == 0, done.stderr
        comments = api.calls('POST', '/repos/o/r/issues/3/comments')
        assert len(comments) == 1, api.requests
        body = comments[0]['data']['body']
        assert 'Build of `linux-modules-nvidia` succeeded in run [42]' in body
        assert 'Closing.' in body
        assert 'actions/runs/42' in body
        patches = api.calls('PATCH', '/repos/o/r/issues/3')
        assert len(patches) == 1, api.requests
        assert patches[0]['data'] == {'state': 'closed'}
    finally:
        api.stop()


def test_issue_writer_stays_english_and_noise_free() -> None:
    """The two steps own the issue strings and both are pure English."""
    flag = _step_run('Flag broken build')
    close = _step_run('Close broken-build issue on success')
    for script in (flag, close):
        assert '/comments' not in script or 'succeeded' in script, (
            'only the close step may comment'
        )
        assert script.count('POST') <= 1
    assert '⚠️ Broken build: ${PKG} (last valid .deb reused)' in flag
    assert 'last valid .deb reused)' in close
