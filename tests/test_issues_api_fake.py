"""Tests for the parts of the fake GitHub Issues API the suite relies on.

The build tests drive the endpoints they need; the fallback answer of the fake
has no caller of its own, so it is checked here.
"""

import http.client
from urllib.parse import urlsplit

import issues_api


def test_unknown_path_answers_an_empty_list() -> None:
    """A path the fake does not model answers an empty JSON list."""
    api = issues_api.FakeIssuesApi()
    connection = http.client.HTTPConnection(urlsplit(api.base_url).netloc)
    try:
        connection.request('GET', '/repos/o/r/other')
        reply = connection.getresponse()
        assert reply.read() == b'[]'
    finally:
        connection.close()
        api.stop()
