"""Shared pytest setup."""

import pytest


@pytest.fixture(autouse=True)
def hermetic_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the AI-agent marker out of the code under test.

    A developer machine may shadow ``sleep`` (and similar tools) with a shim
    that refuses to run whenever ``AI_AGENT`` is set, which is the case when
    the suite is started from an agent session. The scripts under test really
    sleep (retry delays, hung-process timeouts) and the CI never sets the
    marker, so the suite must see the same environment everywhere.

    Args:
        monkeypatch: Pytest environment patcher.
    """
    monkeypatch.delenv('AI_AGENT', raising=False)
