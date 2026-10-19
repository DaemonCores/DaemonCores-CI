# Quality gate of DaemonCores-CI: the shared checks must follow the rules they
# enforce on the callers (strict Google docstrings, typed, formatted).

SHELL := /bin/bash -euo pipefail

VENV = .venv
VBIN = $(VENV)/bin
PYTHON_SRCS := scripts/gen_docs.py scripts/check_docstrings.py \
	scripts/check_docs.py scripts/check_tests_manifest.py \
	scripts/check_build_context.py scripts/check_kconfig.py scripts/patch_guard.py
PYTHON_TESTS := tests
SHELL_SRCS := $(filter-out scripts/build-img.sh,$(wildcard scripts/*.sh))

.PHONY: admission check test

$(VBIN)/python:
	python3 -m venv $(VENV)

admission: $(VBIN)/python
	@$(VBIN)/pip install --quiet --upgrade ruff yapf docformatter mypy \
		basedpyright pydoclint bandit pytest pyyaml types-PyYAML

check: admission
	$(VBIN)/yapf --diff --recursive $(PYTHON_SRCS) $(PYTHON_TESTS)
	$(VBIN)/docformatter --check --diff --recursive $(PYTHON_SRCS)
	$(VBIN)/ruff check $(PYTHON_SRCS) $(PYTHON_TESTS)
	$(VBIN)/mypy --explicit-package-bases $(PYTHON_SRCS)
	$(VBIN)/basedpyright $(PYTHON_SRCS)
	$(VBIN)/pydoclint $(PYTHON_SRCS) $(PYTHON_TESTS)
	$(VBIN)/python scripts/check_docstrings.py --root .
	shellcheck $(SHELL_SRCS)
	shfmt -d -i 2 -bn -ci $(SHELL_SRCS)

test: admission
	$(VBIN)/pytest -q tests
