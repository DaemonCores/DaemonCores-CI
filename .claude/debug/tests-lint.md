# Tests directory not linted (P-974, P-976)

- 2026-11-23 P-974 / P-976: the tests/ directories were never linted by the full gate. Cause (Makefile): docformatter, mypy, basedpyright and bandit ran on the script list only. Counts before: DaemonCores-CI basedpyright 413, mypy 6, bandit medium 11, docformatter 3 files; debian-bootc basedpyright 299, mypy 6, bandit medium 6. Fix: tests/ added to the four tools in both Makefiles, tests typed for real (shared typed helpers, no cast, no ignore, no exec). Proof: make check rc=0 in both repos, tests green (364 and 188 passed).
