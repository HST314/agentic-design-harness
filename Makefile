PYTHON ?= python3
TEST_DEPS ?= .test-deps
TEST_ENV_STAMP := $(TEST_DEPS)/.requirements-dev-installed

.PHONY: test test-env

test: test-env
	PYTHONPATH="$(TEST_DEPS)" $(PYTHON) -m unittest discover -s tests -v

test-env: $(TEST_ENV_STAMP)

$(TEST_ENV_STAMP): requirements-dev.txt
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
		echo "Python interpreter '$(PYTHON)' was not found; override it with 'make test PYTHON=/path/to/python3'." >&2; \
		exit 127; \
	}
	$(PYTHON) -m pip install --disable-pip-version-check --upgrade --target "$(TEST_DEPS)" -r requirements-dev.txt
	@touch $(TEST_ENV_STAMP)
