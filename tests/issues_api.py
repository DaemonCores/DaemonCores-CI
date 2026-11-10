"""Fake GitHub Issues API for the CI test suite.

A tiny threaded HTTP server speaking just enough of the GitHub REST API for
the issue-writing code paths (build-one-deb.yml "Flag broken build" / "Close
broken-build issue on success" and build-deb/build-all.sh ``issue()``): list
open issues, create an issue, update an issue body, post a comment. Every
request handled is recorded in :attr:`requests` so tests assert on the exact
HTTP calls the CI made instead of on side effects in the real GitHub.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class FakeIssuesApi:
    """Serve ``/repos/o/r`` issue endpoints on a local port."""

    def __init__(
        self, repo: str = "o/r", open_issues: list[dict] | None = None
    ) -> None:
        """Start the server and remember the caller's repo and open issues.

        Args:
            repo: ``owner/name`` path served and expected in request URLs.
            open_issues: Issues returned by the :open-issues list endpoint.
        """
        self.repo = repo
        self.open_issues = open_issues if open_issues is not None else []
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):

            def do_GET(self) -> None:
                split = urlsplit(self.path)
                path = split.path.rstrip("/")
                if path == f"/repos/{outer.repo}/issues":
                    body = json.dumps(outer.open_issues).encode()
                    self._reply(body)
                    return
                self._reply(b"[]")
                return

            def do_POST(self) -> None:
                split = urlsplit(self.path)
                path = split.path.rstrip("/")
                length = int(self.headers.get("content-length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({
                    "method": "POST",
                    "path": path,
                    "data": payload
                })
                if path == f"/repos/{outer.repo}/issues":
                    # A created issue joins the open list, like GitHub does.
                    outer.open_issues.append({
                        "number": len(outer.open_issues) + 1,
                        **payload
                    })
                self._reply(b"{}")
                return

            def do_PATCH(self) -> None:
                split = urlsplit(self.path)
                path = split.path.rstrip("/")
                length = int(self.headers.get("content-length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({
                    "method": "PATCH",
                    "path": path,
                    "data": payload
                })
                for index, issue in enumerate(outer.open_issues):
                    if f"/repos/{outer.repo}/issues/{issue['number']}" == path:
                        outer.open_issues[index] = {**issue, **payload}
                self._reply(b"{}")
                return

            def _reply(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return  # keep the pytest output quiet

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        """Root URL to hand to the code under test as GITHUB_API_URL."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def calls(self, method: str, suffix: str) -> list[dict]:
        """Recorded requests by method and path suffix.

        Args:
            method: HTTP method to filter on.
            suffix: Path fragment to filter on, as given.

        Returns:
            The matching recorded requests, in order.
        """
        return [
            call for call in self.requests
            if call["method"] == method and suffix in call["path"]
        ]

    def stop(self) -> None:
        """Shut the server down."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def broken_issue(number: int, package: str) -> dict:
    """An open "Broken build" issue as the CI title-matches it.

    Args:
        number: Issue number the fake API reports.
        package: Package the issue tracks.

    Returns:
        The issue dict.
    """
    return {
        "number": number,
        "title": f"⚠️ Broken build: {package} (last valid .deb reused)"
    }


def actions_env(api: FakeIssuesApi,
                package: str,
                run_id: str = "42") -> dict[str, str]:
    """The Actions environment the issue-writing steps expect.

    Args:
        api: Fake API providing GITHUB_API_URL.
        package: Package under test (PKG).
        run_id: Run id shown in the links.

    Returns:
        The environment variables.
    """
    return {
        "GH_TOKEN": "t",
        "GITHUB_TOKEN": "t",
        "SECRETVAL_GITHUB_TOKEN": "t",
        "PKG": package,
        "GITHUB_API_URL": api.base_url,
        "GITHUB_REPOSITORY": api.repo,
        "GITHUB_SERVER_URL": "http://127.0.0.1:9",
        "GITHUB_RUN_ID": run_id,
    }
