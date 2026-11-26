"""Tests for the workflow iterators of typed_docs.

The empty and missing-sequence paths belong to the helper module: every consumer
iterates a workflow that has jobs and steps, so only these tests exercise the
other branches.
"""

from typing import cast

import pytest
import typed_docs


def test_iter_jobs_without_jobs_yields_nothing() -> None:
    """An empty jobs mapping yields no job."""
    assert list(typed_docs.iter_jobs({'jobs': {}})) == []


def test_iter_steps_without_jobs_yields_nothing() -> None:
    """An empty jobs mapping yields no step."""
    assert list(typed_docs.iter_steps({'jobs': {}})) == []


def test_iter_steps_skips_a_job_without_steps() -> None:
    """A job that declares no steps contributes nothing, the next one does."""
    workflow = cast(
        dict[object, object],
        {'jobs': {
            'no-steps': {},
            'build': {
                'steps': [{
                    'run': 'make'
                }]
            }
        }}
    )
    assert list(typed_docs.iter_steps(workflow)) == [{'run': 'make'}]


def test_a_non_mapping_is_not_a_string_mapping() -> None:
    """Only mappings are string mappings."""
    assert not typed_docs.is_str_mapping([1])


def test_as_str_mapping_refuses_a_non_mapping() -> None:
    """A list is not a mapping, so the narrowing refuses it."""
    with pytest.raises(TypeError, match='expected a mapping with string keys'):
        _ = typed_docs.as_str_mapping([1])


def test_as_mapping_refuses_a_non_mapping() -> None:
    """A string is not a mapping, so the narrowing refuses it."""
    with pytest.raises(TypeError, match='expected a mapping'):
        _ = typed_docs.as_mapping('x')


def test_as_list_refuses_a_non_list() -> None:
    """A string is not a list, so the narrowing refuses it."""
    with pytest.raises(TypeError, match='expected a list'):
        _ = typed_docs.as_list('x')


def test_as_str_refuses_a_non_string() -> None:
    """An integer is not a string, so the narrowing refuses it."""
    with pytest.raises(TypeError, match='expected a string'):
        _ = typed_docs.as_str(3)


def test_as_int_returns_the_typed_value() -> None:
    """An integer passes through the narrowing unchanged."""
    assert typed_docs.as_int(3) == 3


def test_as_int_refuses_a_non_integer() -> None:
    """A numeric string is not an integer, so the narrowing refuses it."""
    with pytest.raises(TypeError, match='expected an integer'):
        _ = typed_docs.as_int('3')


def test_find_mapping_reports_an_absent_value() -> None:
    """No matching mapping is an error, never a silent default."""
    with pytest.raises(LookupError, match='no mapping with name'):
        _ = typed_docs.find_mapping([{'name': 'a'}], 'name', 'b')
