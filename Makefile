PYTHON ?= python3
TEST_DEPS ?= .test-deps
TEST_ENV_STAMP := $(TEST_DEPS)/.requirements-dev-installed
PYTHONPATH_VALUE := backend:$(TEST_DEPS)
IMAGE_AGENT_ROOT ?= $(if $(wildcard agents/image_agent_mvp/requirements.lock),agents/image_agent_mvp,../image_agent_mvp)
IMAGE_AGENT_DEPS ?= .runtime/image-agent-deps
IMAGE_AGENT_ENV_STAMP := $(IMAGE_AGENT_DEPS)/.requirements-installed
REAL_PROVIDER_ENV_FILE ?=
REAL_PROVIDER_EVIDENCE_PATH ?= build/real-provider-evidence.json
REAL_PROVIDER_LOG_FILE ?= build/real-provider-smoke.log
REAL_PROVIDER_ENV_ARG = $(if $(strip $(REAL_PROVIDER_ENV_FILE)),--env-file "$(REAL_PROVIDER_ENV_FILE)",)

.PHONY: test test-env lint typecheck compile secret-scan dependency-audit sbom boundary-check contract-check lock-check docs-check frontend-contracts capacity-benchmark check verify serve frontend-check frontend-unit frontend-e2e frontend-integration real-provider-preflight real-provider-smoke image-agent-env g2-e2e g3-e2e g4-e2e g5-e2e p6-acceptance evidence dev-setup dev-doctor dev-smoke

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

lock-check:
	$(PYTHON) scripts/verify_image_agent_lock.py

dependency-audit: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m pip_audit \
		-r requirements-runtime.txt --require-hashes --disable-pip \
		--progress-spinner off \
		--cache-dir "$(TEST_DEPS)/.pip-audit-cache"
	npm --prefix frontend audit --omit=dev --audit-level=high

sbom: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) scripts/generate_sbom.py

contract-check:
	$(PYTHON) scripts/generate_frontend_contracts.py --check

docs-check:
	$(PYTHON) scripts/check_docs.py

frontend-contracts:
	$(PYTHON) scripts/generate_frontend_contracts.py

capacity-benchmark: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) scripts/benchmark_storage_recovery.py \
		--profile ci --output build/capacity-benchmark.json

frontend-check: contract-check
	npm --prefix frontend run check
	npm --prefix frontend run test:unit
	npm --prefix frontend run build

frontend-unit:
	npm --prefix frontend run test:unit

frontend-e2e:
	npm --prefix frontend run test:e2e

frontend-integration: test-env image-agent-env
	HARNESS_IMAGE_AGENT_ROOT="$(abspath $(IMAGE_AGENT_ROOT))" \
	HARNESS_IMAGE_AGENT_PYTHON="$(shell command -v $(PYTHON))" \
	HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT="$(abspath $(IMAGE_AGENT_DEPS))" \
	PYTHONPATH="$(PYTHONPATH_VALUE)" \
	$(PYTHON) scripts/run_browser_integration.py

real-provider-preflight: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" \
	$(PYTHON) scripts/run_browser_integration.py --real-provider --preflight-only \
		$(REAL_PROVIDER_ENV_ARG)

real-provider-smoke: real-provider-preflight image-agent-env
	HARNESS_IMAGE_AGENT_ROOT="$(abspath $(IMAGE_AGENT_ROOT))" \
	HARNESS_IMAGE_AGENT_PYTHON="$(shell command -v $(PYTHON))" \
	HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT="$(abspath $(IMAGE_AGENT_DEPS))" \
	PYTHONPATH="$(PYTHONPATH_VALUE)" \
	$(PYTHON) scripts/run_browser_integration.py --real-provider \
		$(REAL_PROVIDER_ENV_ARG) \
		--evidence-path "$(REAL_PROVIDER_EVIDENCE_PATH)" \
		--log-file "$(REAL_PROVIDER_LOG_FILE)"

check: test lint compile secret-scan boundary-check lock-check docs-check frontend-check

verify: check typecheck dependency-audit sbom capacity-benchmark

serve: test-env
	PYTHONPATH="$(PYTHONPATH_VALUE)" $(PYTHON) -m harness

dev-setup:
	$(PYTHON) scripts/dev.py setup

dev-doctor:
	$(PYTHON) scripts/dev.py doctor

dev-smoke:
	$(PYTHON) scripts/dev.py start --check

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

g5-e2e:
	G5_MAKE="$(MAKE)" G5_IMAGE_AGENT_ROOT="$(abspath $(IMAGE_AGENT_ROOT))" \
		$(PYTHON) scripts/run_g5_gate.py
	$(PYTHON) scripts/generate_release_evidence.py

p6-acceptance:
	$(PYTHON) scripts/run_p6_platform_acceptance.py

evidence:
	$(PYTHON) scripts/generate_release_evidence.py

test-env: $(TEST_ENV_STAMP)

$(TEST_ENV_STAMP): requirements-dev.txt
	@command -v $(PYTHON) >/dev/null 2>&1 || { \
		echo "Python interpreter '$(PYTHON)' was not found; override it with make PYTHON=/path/to/python3." >&2; \
		exit 127; \
	}
	$(PYTHON) -m pip install --disable-pip-version-check --require-hashes --upgrade \
		--target "$(TEST_DEPS)" -r requirements-dev.txt
	@touch $(TEST_ENV_STAMP)
