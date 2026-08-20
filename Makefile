PYTHON ?= python3
TEST_DEPS ?= .test-deps
TEST_ENV_STAMP := $(TEST_DEPS)/.requirements-dev-installed
PYTHONPATH_VALUE := backend:$(TEST_DEPS)

.PHONY: test test-env lint compile secret-scan boundary-check check serve frontend-check

test: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m unittest discover -s tests -v

lint: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m ruff check backend/harness scripts tests/unit tests/contract tests/integration tests/crash

compile:
	$(PYTHON) -m compileall -q backend/harness scripts tests

secret-scan:
	$(PYTHON) scripts/secret_scan.py .

boundary-check:
	$(PYTHON) scripts/check_agent_import_boundary.py backend/harness

frontend-check:
	npm --prefix frontend run check
	npm --prefix frontend run build

check: test lint compile secret-scan boundary-check frontend-check

serve: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m harness

test-env: $(TEST_ENV_STAMP)

$(TEST_ENV_STAMP): requirements-dev.txt
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
		echo "Python interpreter '$(PYTHON)' was not found; override it with make PYTHON=/path/to/python3." >&2; \
		exit 127; \
	}
	$(PYTHON) -m pip install --disable-pip-version-check --upgrade --target "$(TEST_DEPS)" -r requirements-dev.txt
	@touch $(TEST_ENV_STAMP)
