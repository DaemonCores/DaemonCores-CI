"""Tests of the helper that locates a pre-extraction tree in git history."""

import pytest
from historical import pre_extraction_rev


def test_a_path_no_commit_added_is_reported() -> None:
    """An unknown path is named instead of yielding an empty revision."""
    with pytest.raises(RuntimeError, match='no commit adds no/such/path'):
        _ = pre_extraction_rev('no/such/path')
