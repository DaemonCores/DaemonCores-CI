"""Locate the tree a shared script was extracted from.

Several tests compare a shared script with the inline copy it replaced. The
copy lives in the commit that added the shared path, so that commit's parent is
the exact pre-extraction tree. Deriving it from history keeps the tests free of
a pinned sha: a rewritten or rebased history changes the sha, not the shape of
the tree.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pre_extraction_rev(path: str) -> str:
    """Return the revision of the tree before ``path`` was added.

    Args:
        path: Tracked path whose adding commit marks the extraction.

    Returns:
        Parent revision of the commit that added ``path``.

    Raises:
        RuntimeError: When git knows no commit adding ``path``.
    """
    parents = subprocess.run([
        'git', 'log', '--diff-filter=A', '--format=%P', '-1', '--', path
    ],
                             cwd=ROOT,
                             check=True,
                             capture_output=True,
                             text=True).stdout.split()
    if not parents:
        raise RuntimeError(f'no commit adds {path}')
    return parents[0]
