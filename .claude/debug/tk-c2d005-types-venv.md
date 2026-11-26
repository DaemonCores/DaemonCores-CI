# tk-c2d005 - run 45742556233 (head 36d0d7f): both remaining failures

## lint / types
- Symptom: `gate: types/python FAIL`, 18 errors, all in `tests/test_zfs_module_dep.py`,
  first one `Import "pytest" could not be resolved (reportMissingImports)`.
- Cause, proven locally: `[tool.basedpyright]` pins `venvPath = "."` and
  `venv = ".venv"`. A copy of the tree (`git archive HEAD`) without `.venv`
  yields 465 errors of exactly that family (pytest, mark/skipif, monkeypatch
  unknown; yaml unresolved). The same copy with `.venv` built from
  `requirements-dev.txt` yields `0 errors, 0 warnings`. A fresh runner has no
  `.venv`, so every third-party import is unresolved there.
- Fix: step "Project venv the type checker resolves imports through" in the
  `lint` job of `.github/workflows/tk.yml`: `python3 -m venv .venv` then
  `pip install -r requirements-dev.txt` (the environment the project declares,
  not a hand-picked package list).

## test
- Symptom: the job stops at "Product sibling checkouts":
  `fatal: Remote branch develop not found in upstream origin`, exit 128.
- Cause: `gh repo view` shows debian-bootc default branch `develop`,
  proxmox-ve-bootc `main`; the loop cloned both with `--branch develop`.
- Fix: clone without `--branch` (each product's own default branch).

## Verification
- `tk gate --only lint` exit 0 and `tk gate --only test` exit 0 locally after
  both changes; the run on the pushed commit is the remaining proof.

## Run 45744073658: parity tests, exit 128

- Symptom: 5 tests failed on git show of a pinned sha.
- Cause (proven): the shas are unreachable in a pristine GitHub clone (full depth, 557 commits); history was rewritten and they only survive as local objects.
- Fix: derive the pre-extraction tree as the parent of the commit that added the shared path (tests/historical.py).
