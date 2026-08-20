PYTHON ?= python3
TEST_DEPS ?= .test-deps
TEST_ENV_STAMP := $(TEST_DEPS)/.requirements-dev-installed
PYTHONPATH_VALUE := backend:$(TEST_DEPS)
IMAGE_AGENT_ROOT ?= ../image_agent_mvp
IMAGE_AGENT_DEPS ?= .runtime/image-agent-deps
IMAGE_AGENT_ENV_STAMP := $(IMAGE_AGENT_DEPS)/.requirements-installed

.PHONY: test test-env lint compile secret-scan boundary-check check serve frontend-check image-agent-env g2-e2e g3-e2e

test: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m unittest discover -s tests -v

lint: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m ruff check backend/harness scripts tests/unit tests/contract tests/integration tests/crash tests/e2e

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

image-agent-env: $(IMAGE_AGENT_ENV_STAMP)

$(IMAGE_AGENT_ENV_STAMP): $(IMAGE_AGENT_ROOT)/requirements.lock requirements/image-agent-web.in
	$(PYTHON) -m pip install --disable-pip-version-check --upgrade \
		--target "$(IMAGE_AGENT_DEPS)" -r "$(IMAGE_AGENT_ROOT)/requirements.lock" \
		-r requirements/image-agent-web.in
	@touch "$(IMAGE_AGENT_ENV_STAMP)"

g2-e2e: test-env image-agent-env
	HARNESS_IMAGE_AGENT_ROOT="$(abspath $(IMAGE_AGENT_ROOT))" \
	HARNESS_IMAGE_AGENT_PYTHON="$(shell command -v $(PYTHON))" \
	HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT="$(abspath $(IMAGE_AGENT_DEPS))" \
	PYTHONPATH="$(PYTHONPATH_VALUE):tests" \
	$(PYTHON) -m unittest tests.e2e.test_g2_image_agent -v

g3-e2e: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE):tests" \
	$(PYTHON) -m unittest tests.e2e.test_g3_manual_delivery -v

test-env: $(TEST_ENV_STAMP)

$(TEST_ENV_STAMP): requirements-dev.txt
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
		echo "Python interpreter '$(PYTHON)' was not found; override it with make PYTHON=/path/to/python3." >&2; \
		exit 127; \
	}
	$(PYTHON) -m pip install --disable-pip-version-check --upgrade --target "$(TEST_DEPS)" -r requirements-dev.txt
	@touch $(TEST_ENV_STAMP)
