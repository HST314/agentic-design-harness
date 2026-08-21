PYTHON ?= python3
TEST_DEPS ?= .test-deps
TEST_ENV_STAMP := $(TEST_DEPS)/.requirements-dev-installed
PYTHONPATH_VALUE := backend:$(TEST_DEPS)
IMAGE_AGENT_ROOT ?= ../image_agent_mvp
IMAGE_AGENT_DEPS ?= .runtime/image-agent-deps
IMAGE_AGENT_ENV_STAMP := $(IMAGE_AGENT_DEPS)/.requirements-installed

.PHONY: test test-env lint typecheck compile secret-scan dependency-audit boundary-check check verify serve frontend-check frontend-e2e image-agent-env g2-e2e g3-e2e g4-e2e g5-e2e evidence

test: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m unittest discover -s tests -v

lint: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m ruff check backend/harness scripts tests/unit tests/contract tests/integration tests/crash tests/e2e

typecheck: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m pyright backend/harness

compile:
	$(PYTHON) -m compileall -q backend/harness scripts tests

secret-scan:
	$(PYTHON) scripts/secret_scan.py .

boundary-check:
	$(PYTHON) scripts/check_agent_import_boundary.py backend/harness

dependency-audit: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m pip_audit \
		-r requirements-runtime.txt --disable-pip --no-deps --progress-spinner off \
		--cache-dir "$(TEST_DEPS)/.pip-audit-cache"
	npm --prefix frontend audit --omit=dev --audit-level=high

frontend-check:
	npm --prefix frontend run check
	npm --prefix frontend run build

frontend-e2e:
	npm --prefix frontend run test:e2e

check: test lint compile secret-scan boundary-check frontend-check

verify: check typecheck dependency-audit

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

g3-e2e: test-env image-agent-env
	HARNESS_IMAGE_AGENT_ROOT="$(abspath $(IMAGE_AGENT_ROOT))" \
	HARNESS_IMAGE_AGENT_PYTHON="$(shell command -v $(PYTHON))" \
	HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT="$(abspath $(IMAGE_AGENT_DEPS))" \
	PYTHONPATH="$(PYTHONPATH_VALUE):tests" \
	$(PYTHON) -m unittest tests.e2e.test_g3_manual_delivery \
		tests.e2e.test_g3_real_image_agent -v

g4-e2e: test-env image-agent-env
	HARNESS_IMAGE_AGENT_ROOT="$(abspath $(IMAGE_AGENT_ROOT))" \
	HARNESS_IMAGE_AGENT_PYTHON="$(shell command -v $(PYTHON))" \
	HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT="$(abspath $(IMAGE_AGENT_DEPS))" \
	PYTHONPATH="$(PYTHONPATH_VALUE):tests" \
	$(PYTHON) -m unittest tests.e2e.test_g4_multi_image_agent -v

g5-e2e: verify g3-e2e g4-e2e frontend-e2e evidence

evidence:
	$(PYTHON) scripts/generate_phase1_evidence.py

test-env: $(TEST_ENV_STAMP)

$(TEST_ENV_STAMP): requirements-dev.txt
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
		echo "Python interpreter '$(PYTHON)' was not found; override it with make PYTHON=/path/to/python3." >&2; \
		exit 127; \
	}
	$(PYTHON) -m pip install --disable-pip-version-check --upgrade --target "$(TEST_DEPS)" -r requirements-dev.txt
	@touch $(TEST_ENV_STAMP)
