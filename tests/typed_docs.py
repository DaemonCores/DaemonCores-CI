"""Typed access to documents whose parsers are typed as ``Any``.

The bundled stubs of PyYAML and the stdlib ``json`` module return ``Any`` from
``safe_load`` and ``loads``: the shape is only known at runtime. These helpers
keep that truth explicit — the parsed value crosses into ``object`` once and is
then narrowed with type guards, so a test can never silently use an ``Any``.
"""

import json
from collections.abc import Iterator
from typing import TypeGuard

import yaml


def _yaml_values(text: str) -> Iterator[object]:
    """Yield the document parsed by the untyped YAML loader.

    Args:
        text: YAML source.

    Yields:
        object: The parsed document as an opaque object.
    """
    yield yaml.safe_load(text)


def _json_values(text: str) -> Iterator[object]:
    """Yield the document parsed by the untyped JSON loader.

    Args:
        text: JSON source.

    Yields:
        object: The parsed document as an opaque object.
    """
    yield json.loads(text)


def load_yaml(text: str) -> object:
    """Parse one YAML document without exposing the loader's ``Any``.

    Args:
        text: YAML source.

    Returns:
        The parsed document: mapping, sequence, scalar or ``None``.
    """
    return next(_yaml_values(text))


def load_json(text: str) -> object:
    """Parse one JSON document without exposing the loader's ``Any``.

    Args:
        text: JSON source.

    Returns:
        The parsed document: mapping, sequence, scalar or ``None``.
    """
    return next(_json_values(text))


def is_mapping(value: object) -> TypeGuard[dict[object, object]]:
    """Tell whether a parsed value is a mapping.

    Args:
        value: Any parsed value.

    Returns:
        True when the value is a mapping.
    """
    return isinstance(value, dict)


def is_list(value: object) -> TypeGuard[list[object]]:
    """Tell whether a parsed value is a list.

    Args:
        value: Any parsed value.

    Returns:
        True when the value is a list.
    """
    return isinstance(value, list)


def is_str_mapping(value: object) -> TypeGuard[dict[str, object]]:
    """Tell whether a parsed value is a mapping with string keys.

    Args:
        value: Any parsed value.

    Returns:
        True when the value is a mapping whose keys are all strings.
    """
    if not is_mapping(value):
        return False
    return all(isinstance(key, str) for key in value)


def as_str_mapping(value: object) -> dict[str, object]:
    """Narrow a parsed value to a mapping with string keys.

    Args:
        value: Any parsed value.

    Returns:
        The same value, typed as a mapping with string keys.

    Raises:
        TypeError: When the value is not a mapping with string keys.
    """
    if not is_str_mapping(value):
        raise TypeError(
            " ".join([
                "expected a mapping with string keys, got",
                f"{type(value).__name__}"
            ])
        )
    return value


def as_mapping(value: object) -> dict[object, object]:
    """Narrow a parsed value to a mapping.

    Args:
        value: Any parsed value.

    Returns:
        The same value, typed as a mapping.

    Raises:
        TypeError: When the value is not a mapping.
    """
    if not is_mapping(value):
        raise TypeError(f"expected a mapping, got {type(value).__name__}")
    return value


def as_list(value: object) -> list[object]:
    """Narrow a parsed value to a list.

    Args:
        value: Any parsed value.

    Returns:
        The same value, typed as a list.

    Raises:
        TypeError: When the value is not a list.
    """
    if not is_list(value):
        raise TypeError(f"expected a list, got {type(value).__name__}")
    return value


def as_str(value: object) -> str:
    """Narrow a parsed value to a string.

    Args:
        value: Any parsed value.

    Returns:
        The same value, typed as a string.

    Raises:
        TypeError: When the value is not a string.
    """
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {type(value).__name__}")
    return value


def as_int(value: object) -> int:
    """Narrow a parsed value to an integer.

    Args:
        value: Any parsed value.

    Returns:
        The same value, typed as an integer.

    Raises:
        TypeError: When the value is not an integer.
    """
    if not isinstance(value, int):
        raise TypeError(f"expected an integer, got {type(value).__name__}")
    return value


def iter_jobs(workflow: dict[object, object]) -> Iterator[dict[str, object]]:
    """Yield every job mapping of a GitHub workflow document.

    Args:
        workflow: Workflow document whose ``jobs`` value maps job ids.

    Yields:
        dict[str, object]: Each job, in document order.
    """
    for job in as_mapping(workflow['jobs']).values():
        yield as_str_mapping(job)


def iter_steps(workflow: dict[object, object]) -> Iterator[dict[str, object]]:
    """Yield every step mapping of every job of a workflow document.

    Args:
        workflow: Workflow document whose jobs hold ``steps`` sequences.

    Yields:
        dict[str, object]: Each step, in document order.
    """
    for job in iter_jobs(workflow):
        for step in as_list(job.get('steps', [])):
            yield as_str_mapping(step)


def as_mappings(values: list[object]) -> Iterator[dict[str, object]]:
    """Yield each list item narrowed to a mapping with string keys.

    Args:
        values: A parsed list of values.

    Yields:
        dict[str, object]: Each item, in order.
    """
    for value in values:
        yield as_str_mapping(value)


def find_mapping(values: list[object], field: str,
                 expected: str) -> dict[str, object]:
    """Return the first mapping whose ``field`` equals ``expected``.

    Args:
        values: A parsed list of values.
        field: Key to compare.
        expected: Value the key must hold.

    Returns:
        The matching mapping.

    Raises:
        LookupError: When no mapping carries the expected value.
    """
    for value in as_mappings(values):
        if value.get(field) == expected:
            return value
    raise LookupError(f'no mapping with {field}={expected!r}')


def mappings_by_name(values: list[object]) -> dict[str, dict[str, object]]:
    """Index list items by their string ``name`` field.

    Args:
        values: A parsed list of mappings, each carrying a ``name``.

    Returns:
        A new mapping from each ``name`` to its item.
    """
    by_name: dict[str, dict[str, object]] = {}
    for value in as_mappings(values):
        by_name[as_str(value['name'])] = value
    return by_name
